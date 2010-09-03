# -*- coding: utf-8 -*-
"""
    logbook.ticketing
    ~~~~~~~~~~~~~~~~~

    Implements long handlers that write to remote data stores and assign
    each logging message a ticket id.

    :copyright: (c) 2010 by Armin Ronacher, Georg Brandl.
    :license: BSD, see LICENSE for more details.
"""

import hashlib
from logbook.base import NOTSET, cached_property, _level_name_property, \
     LogRecord
from logbook.handlers import Handler

try:
    import simplejson as json
except ImportError:
    import json


class Ticket(object):
    """Represents a ticket from the database."""

    level_name = _level_name_property()

    def __init__(self, db, row):
        self.db = db
        self.__dict__.update(row)

    @cached_property
    def last_occurrence(self):
        """The last occurrence."""
        rv = self.get_occurrences(limit=1)
        if rv:
            return rv[0]

    def get_occurrences(self, order_by='-time', limit=50, offset=0):
        """Returns the occurrences for this ticket."""
        return self.db.get_occurrences(self.ticket_id)

    def solve(self):
        """Marks this ticket as solved."""
        self.db.solve_ticket(self.ticket_id)
        self.solved = True

    def delete(self):
        """Deletes the ticket from the database."""
        self.db.delete_ticket(self.ticket_id)

    def __eq__(self, other):
        equal = True
        if type(other) != type(self):
            return False
        for key in self.__dict__.keys():
            if getattr(self, key) != getattr(other, key):
                equal = False
                break
        return equal

    def __ne__(self, other):
        return not self.__eq__(other)


class Occurrence(LogRecord):
    """Represents an occurrence of a ticket."""

    def __init__(self, db, row):
        self.update_from_dict(json.loads(row['data']))
        self.db = db
        self.time = row['time']
        self.ticket_id = row['ticket_id']
        self.occurrence_id = row['occurrence_id']


class TicketingDatabase(object):
    """Provides access to the database the :class:`TicketingDatabaseHandler`
    is using.
    """

    def __init__(self, engine_or_uri, table_prefix='logbook_', metadata=None):
        from sqlalchemy import create_engine, MetaData
        if hasattr(engine_or_uri, 'execute'):
            self.engine = engine_or_uri
        else:
            self.engine = create_engine(engine_or_uri, convert_unicode=True)
        if metadata is None:
            metadata = MetaData()
        self.table_prefix = table_prefix
        self.metadata = metadata
        self.create_tables()

    def create_tables(self):
        """Creates the tables required for the handler on the class and
        metadata.
        """
        import sqlalchemy as db
        def table(name, *args, **kwargs):
            return db.Table(self.table_prefix + name, self.metadata,
                            *args, **kwargs)
        self.tickets = table('tickets',
            db.Column('ticket_id', db.Integer, primary_key=True),
            db.Column('record_hash', db.String(40), unique=True),
            db.Column('level', db.Integer),
            db.Column('logger_name', db.String(120)),
            db.Column('location', db.String(512)),
            db.Column('module', db.String(256)),
            db.Column('last_occurrence_time', db.DateTime),
            db.Column('occurrence_count', db.Integer),
            db.Column('solved', db.Boolean),
            db.Column('app_id', db.String(80))
        )
        self.occurrences = table('occurrences',
            db.Column('occurrence_id', db.Integer, primary_key=True),
            db.Column('ticket_id', db.Integer,
                      db.ForeignKey(self.table_prefix + 'tickets.ticket_id')),
            db.Column('time', db.DateTime),
            db.Column('data', db.Text),
            db.Column('app_id', db.String(80))
        )

    def _order(self, q, table, order_by):
        if order_by[0] == '-':
            return q.order_by(table.c[order_by[1:]].desc())
        return q.order_by(table.c[order_by])

    def record_ticket(self, record, data, hash, app_id):
        """Records a log record as ticket."""
        cnx = self.engine.connect()
        trans = cnx.begin()
        try:
            q = self.tickets.select(self.tickets.c.record_hash == hash)
            row = cnx.execute(q).fetchone()
            if row is None:
                row = cnx.execute(self.tickets.insert().values(
                    record_hash=hash,
                    level=record.level,
                    logger_name=record.logger_name or u'',
                    location=u'%s:%d' % (record.filename, record.lineno),
                    module=record.module or u'<unknown>',
                    occurrence_count=0,
                    solved=False,
                    app_id=app_id
                ))
                ticket_id = row.inserted_primary_key[0]
            else:
                ticket_id = row['ticket_id']
            cnx.execute(self.occurrences.insert()
                .values(ticket_id=ticket_id,
                        time=record.time,
                        app_id=app_id,
                        data=json.dumps(data)))
            cnx.execute(self.tickets.update()
                .where(self.tickets.c.ticket_id == ticket_id)
                .values(occurrence_count=self.tickets.c.occurrence_count + 1,
                        last_occurrence_time=record.time,
                        solved=False))
            trans.commit()
        except Exception:
            trans.rollback()
            raise
        cnx.close()

    def count_tickets(self):
        """Returns the number of tickets."""
        return self.engine.execute(self.tickets.count()).fetchone()[0]

    def get_tickets(self, order_by='-last_occurrence_time', limit=50, offset=0):
        """Selects tickets from the database."""
        return [Ticket(self, row) for row in self.engine.execute(
            self._order(self.tickets.select(), self.tickets, order_by)
            .limit(limit).offset(offset)).fetchall()]

    def solve_ticket(self, ticket_id):
        """Marks a ticket as solved."""
        self.engine.execute(self.tickets.update()
            .where(self.tickets.c.ticket_id == ticket_id)
            .values(solved=True))

    def delete_ticket(self, ticket_id):
        """Deletes a ticket from the database."""
        self.engine.execute(self.occurrences.delete()
            .where(self.occurrences.c.ticket_id == ticket_id))
        self.engine.execute(self.tickets.delete()
            .where(self.tickets.c.ticket_id == ticket_id))

    def get_ticket(self, ticket_id):
        """Return a single ticket with all occurrences."""
        row = self.engine.execute(self.tickets.select().where(
            self.tickets.c.ticket_id == ticket_id)).fetchone()
        if row is not None:
            return Ticket(self, row)

    def get_occurrences(self, ticket, order_by='-time', limit=50, offset=0):
        """Selects occurrences from the database for a ticket."""
        return [Occurrence(self, row) for row in
                self.engine.execute(self._order(self.occurrences.select()
                    .where(self.occurrences.c.ticket_id == ticket),
                    self.occurrences, order_by)
                .limit(limit).offset(offset)).fetchall()]


class TicketingBaseHandler(Handler):
    """Baseclass for all ticketing handlers."""

    def hash_record(self, record):
        """Returns the unique hash of a record."""
        hash = hashlib.sha1()
        hash.update('%d\x00' % record.level)
        hash.update((record.logger_name or u'').encode('utf-8') + '\x00')
        hash.update(record.filename.encode('utf-8') + '\x00')
        hash.update(str(record.lineno))
        if record.module:
            hash.update('\x00' + record.module)
        if self.hash_salt is not None:
            hash.update('\x00' + self.hash_salt)
        return hash.hexdigest()

    def process_record(self, record, hash):
        """Subclasses can override this to tamper with the data dict that
        is sent to the database as JSON.
        """
        return record.to_dict(json_safe=True)

    def record_ticket(self, record, data, hash):
        """Has to be implemented by subclasses and record either a new
        ticket or a new occurrence for a ticket based on the hash.
        """
        raise NotImplementedError()

    def emit(self, record):
        """Emits a single record and writes it to the database."""
        hash = self.hash_record(record)
        data = self.process_record(record, hash)
        self.record_ticket(record, data, hash)


class TicketingDatabaseHandler(TicketingBaseHandler):
    """A handler that writes log records into a remote database.  This
    database can be connected to from different dispatchers which makes
    this a nice setup for web applications::

        from logbook.ticketing import TicketingDatabaseHandler
        handler = TicketingDatabaseHandler('sqlite:////tmp/myapp-logs.db')

    :param engine_or_uri: a SQLAlchemy engine object or a string with an
                          SQLAlchemy database connection URI
    :param app_id: a string with an optional ID for an application.  Can be
                   used to keep multiple application setups apart when logging
                   into the same database.
    :param table_prefix: an optional table prefix for all tables created by
                         the logbook ticketing handler.
    :param metadata: an optional SQLAlchemy metadata object for the table
                     creation.
    :param autocreate_tables: can be set to `False` to disable the automatic
                              creation of the logbook tables.
    :param hash_salt: an optional salt (binary string) for the hashes.
    """

    def __init__(self, engine_or_uri, app_id='generic', table_prefix='logbook_',
                 metadata=None, autocreate_tables=True, hash_salt=None,
                 level=NOTSET, filter=None, bubble=False):
        Handler.__init__(self, level, filter, bubble)
        #: The :class:`TicketingDatabase` for this handler.
        self.db = TicketingDatabase(engine_or_uri, table_prefix, metadata)
        #: the application ID that is recorded.
        self.app_id = app_id
        #: the salt for the hashes.
        self.hash_salt = hash_salt or app_id.encode('utf-8')
        if autocreate_tables:
            self.db.metadata.create_all(bind=self.db.engine)

    def record_ticket(self, record, data, hash):
        self.db.record_ticket(record, data, hash, self.app_id)
