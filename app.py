import hmac
import os
import re
from datetime import datetime, timezone
from uuid import uuid4

import click
from flask import Flask, jsonify, request
from sqlalchemy import create_engine, String, Text, DateTime, select, func
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, Session
from werkzeug.exceptions import HTTPException

CATEGORIES = ('Hardware', 'Software', 'Red', 'Accesos', 'Otro')
PRIORITIES = ('Baja', 'Media', 'Alta', 'Crítica')
STATES = ('Pendiente', 'En proceso', 'Resuelta')

class Base(DeclarativeBase):
    pass

class Ticket(Base):
    __tablename__ = 'solicitudes'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    folio: Mapped[str] = mapped_column(String(48), unique=True)
    source_id: Mapped[str] = mapped_column(String(200), unique=True)
    nombre: Mapped[str] = mapped_column(String(120))
    correo: Mapped[str] = mapped_column(String(254))
    categoria: Mapped[str] = mapped_column(String(30))
    prioridad: Mapped[str] = mapped_column(String(20))
    descripcion: Mapped[str] = mapped_column(Text)
    estado: Mapped[str] = mapped_column(String(20), default='Pendiente')
    creado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    def data(self):
        d = {c.name: getattr(self, c.name) for c in self.__table__.columns}
        d['creado_en'] = self.creado_en.replace(tzinfo=timezone.utc).isoformat()
        return d

def create_app(config=None):
    app = Flask(__name__)
    app.config.update(API_KEY=os.getenv('API_KEY', ''), DATABASE_URL=os.getenv('DATABASE_URL', ''), MAX_CONTENT_LENGTH=32*1024)
    app.config.update(config or {})
    if len(app.config['API_KEY']) < 32:
        raise RuntimeError('Define API_KEY con al menos 32 caracteres.')
    url = app.config['DATABASE_URL']
    if not url:
        raise RuntimeError('Define DATABASE_URL. Para demo: sqlite:///demo.db')
    if url.startswith(('postgres://', 'postgresql://')):
        url = 'postgresql+psycopg://' + url.split('://', 1)[1]
    options = {'connect_timeout': 10} if url.startswith('postgresql') else {}
    engine = create_engine(url, pool_pre_ping=True, connect_args=options)
    app.extensions['db_engine'] = engine

    @app.cli.command('init-db')
    def init_db():
        Base.metadata.create_all(engine)
        click.echo('Esquema creado; no se modificaron tablas existentes.')

    @app.before_request
    def auth():
        if request.path in ('/', '/health'):
            return
        expected = 'Bearer ' + app.config['API_KEY']
        if not hmac.compare_digest(request.headers.get('Authorization', '').encode('utf-8'), expected.encode('utf-8')):
            return jsonify(error='No autorizado'), 401

    @app.errorhandler(HTTPException)
    def http_error(e):
        return jsonify(error=e.description), e.code

    @app.errorhandler(SQLAlchemyError)
    def db_error(e):
        app.logger.error('Error de base de datos: %s', type(e).__name__)
        return jsonify(error='Base de datos no disponible'), 503

    @app.get('/')
    def index():
        return jsonify(servicio='Gestión de solicitudes', version='1.0', health='/health')

    @app.get('/health')
    def health():
        with engine.connect() as conn:
            conn.execute(select(1))
            conn.execute(select(Ticket.id).limit(1))
        return jsonify(status='ok')

    def body():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return None
        return data

    @app.post('/api/solicitudes')
    def create_ticket():
        d = body()
        if d is None:
            return jsonify(error='Se requiere un objeto JSON'), 400
        fields = {'source_id':200, 'nombre':120, 'correo':254, 'categoria':30, 'prioridad':20, 'descripcion':5000}
        clean = {}
        for field, maximum in fields.items():
            value = d.get(field)
            if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
                return jsonify(error=f'{field}: texto obligatorio, máximo {maximum} caracteres'), 400
            clean[field] = value.strip()
        if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', clean['correo']):
            return jsonify(error='Correo inválido'), 400
        if clean['categoria'] not in CATEGORIES or clean['prioridad'] not in PRIORITIES:
            return jsonify(error='Categoría o prioridad inválida', categorias=CATEGORIES, prioridades=PRIORITIES), 400
        clean['correo'] = clean['correo'].lower()
        with Session(engine) as s:
            existing = s.scalar(select(Ticket).where(Ticket.source_id == clean['source_id']))
            if existing:
                return replay(existing, clean)
            identifier = str(uuid4())
            ticket = Ticket(id=identifier, folio='ST-' + datetime.now(timezone.utc).strftime('%Y%m%d') + '-' + identifier.replace('-', '').upper(), creado_en=datetime.now(timezone.utc), **clean)
            s.add(ticket)
            try:
                s.commit()
            except IntegrityError:
                s.rollback()
                existing = s.scalar(select(Ticket).where(Ticket.source_id == clean['source_id']))
                if existing:
                    return replay(existing, clean)
                raise
            return jsonify(ticket.data()), 201

    def replay(existing, clean):
        if any(getattr(existing, k) != v for k, v in clean.items()):
            return jsonify(error='source_id ya existe con otros datos'), 409
        return jsonify(existing.data()), 200

    @app.get('/api/solicitudes')
    def list_tickets():
        try:
            page = int(request.args.get('page', '1'))
            limit = int(request.args.get('limit', '20'))
            if page < 1 or not 1 <= limit <= 100:
                raise ValueError()
        except ValueError:
            return jsonify(error='page >= 1; limit entre 1 y 100'), 400
        query = select(Ticket)
        for field, allowed in [('categoria', CATEGORIES), ('prioridad', PRIORITIES), ('estado', STATES)]:
            if field in request.args:
                if request.args[field] not in allowed:
                    return jsonify(error=f'{field} inválido'), 400
                query = query.where(getattr(Ticket, field) == request.args[field])
        with Session(engine) as s:
            total = s.scalar(select(func.count()).select_from(query.subquery()))
            rows = s.scalars(query.order_by(Ticket.creado_en.desc(), Ticket.id).offset((page-1)*limit).limit(limit))
            return jsonify(items=[t.data() for t in rows], total=total, page=page, limit=limit)

    @app.get('/api/solicitudes/<folio>')
    def get_ticket(folio):
        with Session(engine) as s:
            ticket = s.scalar(select(Ticket).where(Ticket.folio == folio))
            return (jsonify(ticket.data()), 200) if ticket else (jsonify(error='No encontrada'), 404)

    @app.patch('/api/solicitudes/<folio>')
    def update_ticket(folio):
        d = body()
        if not d or d.get('estado') not in STATES or set(d) != {'estado'}:
            return jsonify(error='Envía únicamente estado: Pendiente, En proceso o Resuelta'), 400
        with Session(engine) as s:
            ticket = s.scalar(select(Ticket).where(Ticket.folio == folio))
            if not ticket:
                return jsonify(error='No encontrada'), 404
            ticket.estado = d['estado']
            s.commit()
            return jsonify(ticket.data())

    @app.get('/api/dashboard')
    def dashboard():
        with Session(engine) as s:
            def counts(column):
                return dict(s.execute(select(column, func.count()).group_by(column)).all())
            return jsonify(total=s.scalar(select(func.count()).select_from(Ticket)), por_categoria=counts(Ticket.categoria), por_prioridad=counts(Ticket.prioridad), por_estado=counts(Ticket.estado))
    return app
