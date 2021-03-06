# coding=utf-8
"""
Core SQL schema settings.
"""
from __future__ import absolute_import

import logging

from sqlalchemy import MetaData
from sqlalchemy.schema import CreateSchema

from ._sql import TYPES_INIT_SQL

USER_ROLES = ('agdc_user', 'agdc_ingest', 'agdc_manage', 'agdc_admin')

SQL_NAMING_CONVENTIONS = {
    "ix": 'ix_%(column_0_label)s',
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
    # Other prefixes handled outside of sqlalchemy:
    # dix: dynamic-index, those indexes created automatically based on search field configuration.
    # tix: test-index, created by hand for testing, particularly in dev.
}
SCHEMA_NAME = 'agdc'
METADATA = MetaData(naming_convention=SQL_NAMING_CONVENTIONS, schema=SCHEMA_NAME)
S3_METADATA = MetaData(naming_convention=SQL_NAMING_CONVENTIONS, schema=SCHEMA_NAME)

_LOG = logging.getLogger(__name__)


def schema_qualified(name):
    """
    >>> schema_qualified('dataset')
    'agdc.dataset'
    """
    return '{}.{}'.format(SCHEMA_NAME, name)


def _get_quoted_connection_info(connection):
    db, user = connection.execute("select quote_ident(current_database()), quote_ident(current_user)").fetchone()
    return db, user


def ensure_db(engine, with_permissions=True, with_s3_tables=False):
    """
    Initialise the db if needed.
    """
    is_new = False
    c = engine.connect()

    quoted_db_name, quoted_user = _get_quoted_connection_info(c)

    if with_permissions:
        _LOG.info('Ensuring user roles.')
        _ensure_role(c, 'agdc_user')
        _ensure_role(c, 'agdc_ingest', inherits_from='agdc_user')
        _ensure_role(c, 'agdc_manage', inherits_from='agdc_ingest')
        _ensure_role(c, 'agdc_admin', inherits_from='agdc_manage', add_user=True)

        c.execute("""
        grant all on database {db} to agdc_admin;
        """.format(db=quoted_db_name))

    if not has_schema(engine, c):
        is_new = True
        try:
            c.execute('begin')
            if with_permissions:
                # Switch to 'agdc_admin', so that all items are owned by them.
                c.execute('set role agdc_admin')
            _LOG.info('Creating schema.')
            c.execute(CreateSchema(SCHEMA_NAME))
            _LOG.info('Creating tables.')
            c.execute(TYPES_INIT_SQL)
            METADATA.create_all(c)
            if with_s3_tables:
                S3_METADATA.create_all(c)
            c.execute('commit')
        except:
            c.execute('rollback')
            raise
        finally:
            if with_permissions:
                c.execute('set role "{}"'.format(quoted_user))

    if with_permissions:
        _LOG.info('Adding role grants.')
        c.execute("""
        grant usage on schema {schema} to agdc_user;
        grant select on all tables in schema {schema} to agdc_user;
        grant execute on function {schema}.common_timestamp(text) to agdc_user;

        grant insert on {schema}.dataset,
                        {schema}.dataset_location,
                        {schema}.dataset_source to agdc_ingest;
        grant usage, select on all sequences in schema {schema} to agdc_ingest;

        -- (We're only granting deletion of types that have nothing written yet: they can't delete the data itself)
        grant insert, delete on {schema}.dataset_type,
                                {schema}.metadata_type to agdc_manage;
        -- Allow creation of indexes, views
        grant create on schema {schema} to agdc_manage;
        """.format(schema=SCHEMA_NAME))

    c.close()

    return is_new


def _pg_exists(conn, name):
    """
    Does a postgres object exist?
    :rtype bool
    """
    return conn.execute("SELECT to_regclass(%s)", name).scalar() is not None


def _pg_column_exists(conn, table, column):
    """
    Does a postgres object exist?
    :rtype bool
    """
    return conn.execute("""
                        select 1 from pg_attribute
                        where attrelid = to_regclass(%s)
                        and attname = %s
                        and not attisdropped
                        """, table, column).scalar() is not None


def database_exists(engine):
    """
    Have they init'd this database?
    """
    return has_schema(engine, engine)


def schema_is_latest(engine):
    """
    Is the schema up-to-date?
    """
    # We may have versioned schema in the future.
    # For now, we know updates have been applied if certain objects exist,

    location_first_index = 'ix_{schema}_dataset_location_dataset_ref'.format(schema=SCHEMA_NAME)

    has_dataset_source_update = not _pg_exists(engine, schema_qualified('uq_dataset_source_dataset_ref'))
    has_uri_searches = _pg_exists(engine, schema_qualified(location_first_index))
    has_dataset_location = _pg_column_exists(engine, schema_qualified('dataset_location'), 'archived')
    return has_dataset_source_update and has_uri_searches and has_dataset_location


def update_schema(engine):
    is_unification = _pg_exists(engine, schema_qualified('dataset_type'))
    if not is_unification:
        raise ValueError('Pre-unification database cannot be updated.')

    # Removal of surrogate key from dataset_source: it makes the table larger for no benefit.
    if _pg_exists(engine, schema_qualified('uq_dataset_source_dataset_ref')):
        _LOG.info('Applying surrogate-key update')
        engine.execute("""
        begin;
          alter table {schema}.dataset_source drop constraint pk_dataset_source;
          alter table {schema}.dataset_source drop constraint uq_dataset_source_dataset_ref;
          alter table {schema}.dataset_source add constraint pk_dataset_source primary key(dataset_ref, classifier);
          alter table {schema}.dataset_source drop column id;
        commit;
        """.format(schema=SCHEMA_NAME))
        _LOG.info('Completed surrogate-key update')

    # float8range is needed if the user uses the double-range field type.
    if not engine.execute("SELECT 1 FROM pg_type WHERE typname = 'float8range'").scalar():
        engine.execute(TYPES_INIT_SQL)

    if not _pg_column_exists(engine, schema_qualified('dataset_location'), 'archived'):
        _LOG.info('Applying dataset_location.archived update')
        engine.execute("""
          alter table {schema}.dataset_location add column archived TIMESTAMP WITH TIME ZONE
          """.format(schema=SCHEMA_NAME))
        _LOG.info('Completed dataset_location.archived update')

    # Update uri indexes to allow dataset search-by-uri.
    if not _pg_exists(engine, schema_qualified('ix_{schema}_dataset_location_dataset_ref'.format(schema=SCHEMA_NAME))):
        _LOG.info('Applying uri-search update')
        engine.execute("""
        begin;
          -- Add a separate index by dataset.
          create index ix_{schema}_dataset_location_dataset_ref on {schema}.dataset_location (dataset_ref);

          -- Replace (dataset, uri) index with (uri, dataset) index.
          alter table {schema}.dataset_location add constraint uq_dataset_location_uri_scheme unique (uri_scheme, uri_body, dataset_ref);
          alter table {schema}.dataset_location drop constraint uq_dataset_location_dataset_ref;
        commit;
        """.format(schema=SCHEMA_NAME))
        _LOG.info('Completed uri-search update')


def _ensure_role(engine, name, inherits_from=None, add_user=False, create_db=False):
    if has_role(engine, name):
        _LOG.debug('Role exists: %s', name)
        return

    sql = [
        'create role %s nologin inherit' % name,
        'createrole' if add_user else 'nocreaterole',
        'createdb' if create_db else 'nocreatedb'
    ]
    if inherits_from:
        sql.append('in role ' + inherits_from)
    engine.execute(' '.join(sql))


def _escape_pg_identifier(engine, name):
    # New (2.7+) versions of psycopg2 have function: extensions.quote_ident()
    # But it's too bleeding edge right now. We'll ask the server to escape instead, as
    # these are not performance sensitive.
    return engine.execute("select quote_ident(%s)", name).scalar()


def create_user(conn, username, key, role, description=None):
    if role not in USER_ROLES:
        raise ValueError('Unknown role %r. Expected one of %r' % (role, USER_ROLES))
    username = _escape_pg_identifier(conn, username)
    conn.execute(
        'create user {username} password %s in role {role}'.format(username=username, role=role),
        key
    )
    if description:
        conn.execute(
            'comment on role {username} is %s'.format(username=username), description
        )


def drop_user(engine, *usernames):
    for username in usernames:
        engine.execute('drop role {username}'.format(username=_escape_pg_identifier(engine, username)))


def grant_role(engine, role, users):
    if role not in USER_ROLES:
        raise ValueError('Unknown role %r. Expected one of %r' % (role, USER_ROLES))

    users = [_escape_pg_identifier(engine, user) for user in users]
    with engine.begin():
        engine.execute('revoke {roles} from {users}'.format(users=', '.join(users), roles=', '.join(USER_ROLES)))
        engine.execute('grant {role} to {users}'.format(users=', '.join(users), role=role))


def has_role(conn, role_name):
    return bool(conn.execute('select rolname from pg_roles where rolname=%s', role_name).fetchall())


def has_schema(engine, connection):
    return engine.dialect.has_schema(connection, SCHEMA_NAME)


def drop_db(connection):
    connection.execute('drop schema if exists %s cascade;' % SCHEMA_NAME)


def to_pg_role(role):
    """
    >>> to_pg_role('ingest')
    'agdc_ingest'
    >>> to_pg_role('fake')
    Traceback (most recent call last):
    ...
    ValueError: Unknown role 'fake'. Expected one of ...
    """
    pg_role = 'agdc_' + role.lower()
    if pg_role not in USER_ROLES:
        raise ValueError(
            'Unknown role %r. Expected one of %r' %
            (role, [r.split('_')[1] for r in USER_ROLES])
        )
    return pg_role


def from_pg_role(pg_role):
    """
    >>> from_pg_role('agdc_admin')
    'admin'
    >>> from_pg_role('fake')
    Traceback (most recent call last):
    ...
    ValueError: Not a pg role: 'fake'. Expected one of ...
    """
    if pg_role not in USER_ROLES:
        raise ValueError('Not a pg role: %r. Expected one of %r' % (pg_role, USER_ROLES))

    return pg_role.split('_')[1]
