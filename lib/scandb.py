#!/usr/bin/env python
"""
SQLAlchemy wrapping of scan database

Main Class for full Database:  ScanDB
"""
from __future__ import print_function
import os
import sys
import json
import time
import atexit
import logging
import numpy as np
from socket import gethostname
from datetime import datetime
import yaml
# from utils import backup_versions, save_backup
import sqlalchemy
from sqlalchemy import MetaData, Table, select, and_, create_engine, text
from sqlalchemy.orm import sessionmaker, mapper, clear_mappers

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import  NoResultFound

# needed for py2exe?
from sqlalchemy.dialects import sqlite, postgresql

import epics

from .scandb_schema import get_dbengine, create_scandb, map_scandb
from .scandb_schema import (Info, Status, PV, MonitorValues, ExtraPVs,
                            Macros, Commands, ScanData, ScanPositioners,
                            ScanCounters, ScanDetectors, ScanDefs,
                            SlewScanPositioners, Position, Position_PV,
                            Instrument, Instrument_PV, Common_Commands,
                            Instrument_Precommands, Instrument_Postcommands)


from .utils import (normalize_pvname, asciikeys, pv_fullname,
                    ScanDBException, ScanDBAbort)
from .create_scan import create_scan

def get_credentials(envvar='ESCAN_CREDENTIALS'):
    """look up credentials file from environment variable"""
    conn = {}
    credfile = os.environ.get(envvar, None)
    if credfile is not None and os.path.exists(credfile):
        with open(credfile, 'r') as fh:
            text = fh.read()
            text.replace('=', ': ')
            conn = yaml.load(text, Loader=yaml.Loader)
    return conn

def json_encode(val):
    "simple wrapper around json.dumps"
    if val is None or isinstance(val, (str, unicode)):
        return val
    return  json.dumps(val)

def isotime2datetime(isotime):
    "convert isotime string to datetime object"
    sdate, stime = isotime.replace('T', ' ').split(' ')
    syear, smon, sday = [int(x) for x in sdate.split('-')]
    sfrac = '0'
    if '.' in stime:
        stime, sfrac = stime.split('.')
    shour, smin, ssec  = [int(x) for x in stime.split(':')]
    susec = int(1e6*float('.%s' % sfrac))
    return datetime(syear, smon, sday, shour, smin, ssec, susec)

def make_datetime(t=None, iso=False):
    """unix timestamp to datetime iso format
    if t is None, current time is used"""
    if t is None:
        dt = datetime.now()
    else:
        dt = datetime.utcfromtimestamp(t)
    if iso:
        return datetime.isoformat(dt)
    return dt

def None_or_one(val, msg='Expected 1 or None result'):
    """expect result (as from query.all() to return
    either None or exactly one result
    """
    if len(val) == 1:
        return val[0]
    elif len(val) == 0:
        return None
    else:
        raise ScanDBException(msg)

def save_sqlite(filename, dbname=None, server='postgresql', **kws):
    """save scandb to sqlite format

    Arguments
    ---------
    filename  name of sqlite3 database to write -- will be clobbered if it exists
    dbname    name of database
    server    server type (only postgresql supported)
    """
    if server.startswith('sqlit'):
        raise ValueError("no need to save sqlite db to sqlite!")

    pg_scandb = ScanDB(dbname=dbname, server=server, **kws)
    if os.path.exists(filename):
        os.unlink(filename)
        time.sleep(0.5)

    tablenames = ('info', 'config', 'slewscanpositioners', 'scanpositioners',
                  'scancounters', 'scandetectors', 'scandefs', 'extrapvs',
                  'macros', 'pv', 'instrument', 'position', 'instrument_pv',
                  'position_pv', 'commands', 'common_commands')

    rows, cols = {}, {}
    n = 0
    for tname in tablenames:
        allrows = pg_scandb.select(tname)
        if len(allrows) > 0:
            cols[tname] = allrows[0].keys()
            rows[tname] = [[item for item in row] for row in allrows]
            n += 1
            if n % 10000 == 0:
                print('', end='.')
                sys.stdout.flush()

    pg_scandb.close()
    clear_mappers()

    sdb = ScanDB(dbname=filename, server='sqlite3', create=True)
    sdb.clobber_all_info()
    sdb.commit()
    for tname in tablenames:
        if tname not in rows:
            continue
        cls, table = sdb.get_table(tname)
        ckeys = cols[tname]
        for row in rows[tname]:
            kws = {}
            for k, v in zip(ckeys, row):
                kws[k] = v
                n += 1
                if n % 10000 == 0:
                    print('', end='.')
                    sys.stdout.flush()
            table.insert().execute(**kws)
    sdb.commit()
    print(" Wrote %s " % filename)

class ScanDB(object):
    """
    Main Interface to Scans Database
    """
    def __init__(self, dbname=None, server='sqlite', create=False, **kws):
        self.dbname = dbname
        if server == 'sqlite3':
            server = 'sqlite'
        self.server = server
        self.tables = None
        self.engine = None
        self.session = None
        self.conn    = None
        self.metadata = None
        self.pvs = {}
        self.scandata = []
        self.restoring_pvs = []
        if dbname is None:
            conndict = get_credentials(envvar='ESCAN_CREDENTIALS')
            if 'dbname' in conndict:
                self.dbname = conndict.pop('dbname')
            if 'server' in conndict:
                self.server = conndict.pop('server')
            kws.update(conndict)
        if self.dbname is not None:
            self.connect(self.dbname, server=self.server,
                         create=create, **kws)

    def create_newdb(self, dbname, connect=False, **kws):
        "create a new, empty database"
        create_scandb(dbname,  **kws)
        if connect:
            time.sleep(0.5)
            self.connect(dbname, backup=False, **kws)

    def set_path(self, fileroot=None):
        workdir = self.get_info('user_folder')
        workdir = workdir.replace('\\', '/').replace('//', '/')
        if workdir.startswith('/'):
            workdir = workdir[1:]
        if fileroot is None:
            fileroot = self.get_info('server_fileroot')
            if os.name == 'nt':
                fileroot = self.get_info('windows_fileroot')
                if not fileroot.endswith('/'):
                    fileroot += '/'
            fileroot = fileroot.replace('\\', '/').replace('//', '/')
        if workdir.startswith(fileroot):
            workdir = workdir[len(fileroot):]

        fullpath = os.path.join(fileroot, workdir)
        fullpath = fullpath.replace('\\', '/').replace('//', '/')
        try:
            os.chdir(fullpath)
        except:
            logging.exception("ScanDB: Could not set working directory to %s " % fullpath)
        finally:
            # self.set_info('server_fileroot',  fileroot)
            self.set_info('user_folder',      workdir)
        time.sleep(0.1)

    def isScanDB(self, dbname, server='sqlite',
                 user='', password='', host='', port=None):
        """test if a file is a valid scan database:
        must be a sqlite db file, with tables named
        'postioners', 'detectors', and 'scans'
        """
        if server.startswith('sqlite'):
            if not os.path.exists(dbname):
                return False
        else:
            if port is None:
                if server.startswith('my'): port = 3306
                if server.startswith('p'):  port = 5432
            #conn = "%s://%s:%s@%s:%i/%s"
            #try:
            #    _db = create_engine(conn % (server, user, password,
            #                                host, port, dbname))
            #except:
            #   return False

        _tables = ('info', 'status', 'commands', 'pv', 'scandefs')
        engine = get_dbengine(dbname, server=server, create=False,
                              user=user, password=password,
                              host=host, port=port)
        try:
            meta = MetaData(engine)
            meta.reflect()
        except:
            engine, meta = None, None
            return False

        allfound = False
        if all([t in meta.tables for t in _tables]):
            keys = [row.key for row in
                    meta.tables['info'].select().execute().fetchall()]
            allfound = 'version' in keys and 'experiment_id' in keys
        if allfound:
            self.engine = engine
            self.dbname = dbname
            self.metadata = meta
        return allfound

    def connect(self, dbname, server='sqlite', create=False,
                user='', password='', host='', port=None, **kws):
        "connect to an existing database"
        creds = dict(user=user, password=password, host=host,
                     port=port, server=server)
        self.dbname = dbname
        if not self.isScanDB(dbname,  **creds) and create:
            engine, meta = create_scandb(dbname, create=True, **creds)
            self.engine = engine
            self.metadata = meta
            self.metadata.reflect()

        if self.engine is None:
            raise ValueError("Cannot use '%s' as a Scan Database!" % dbname)

        self.conn   = self.engine.connect()
        self.session = sessionmaker(bind=self.engine, autocommit=True)()

        tabs, classes, mapprops, mapkeys = map_scandb(self.metadata)
        self.tables, self.classes = tabs, classes
        self.mapprops, self.mapkeys = mapprops, mapkeys

        self.status_codes = {}
        self.status_names = {}
        for row in self.getall('status'):
            self.status_codes[row.name] = row.id
            self.status_names[row.id] = row.name
        # atexit.register(self.close)

    def commit(self):
        "commit session state -- null op since using autocommit"
        self.session.flush()

    def close(self):
        "close session"
        try:
            self.set_hostpid(clear=True)
            self.session.flush()
            self.session.close()
            self.conn.close()
        except:
            logging.exception("could not close session")

    def query(self, *args, **kws):
        "generic query"
        try:
            return self.session.query(*args, **kws)
        except sqlalchemy.exc.StatementError():
            time.sleep(0.01)
            self.session.rollback()
            time.sleep(0.01)
            try:
                return self.session.query(*args, **kws)
            except:
                self.session.rollback()
                logging.exception("rolling back session at query", args, kws)
                return None
        # self.session.autoflush = True

    def _get_table(self, tablename):
        return self.get_table(tablename)

    def get_table(self, tablename):
        "return (self.tables, self.classes) for a table name"
        cls   = self.classes[tablename]
        table = self.tables[tablename]
        attr  = self.mapkeys[tablename]
        props = self.mapprops[tablename]
        if not hasattr(cls , attr):
            mapper(cls, table, props)
        return cls, table

    def getall(self, tablename, orderby=None):
        """return objects for all rows from a named table
         orderby   to order results
        """
        cls, table = self.get_table(tablename)
        columns = table.c.keys()
        q = self.query(cls)
        if orderby is not None and hasattr(cls, orderby):
            q = q.order_by(getattr(cls, orderby))
        return q.all()

    def select(self, tablename, orderby=None, **kws):
        """return data for all rows from a named table,
         orderby   to order results
         key=val   to get entries matching a column (where clause)
        """
        cls, table = self.get_table(tablename)
        columns = table.c.keys()
        q = table.select()
        for key, val in kws.items():
            if key in columns:
                q = q.where(getattr(table.c, key)==val)
        if orderby is not None and hasattr(cls, orderby):
            q = q.order_by(getattr(cls, orderby))
        return q.execute().fetchall()

    def get_info(self, key=None, default=None, prefix=None,
                 as_int=False, as_bool=False, orderby='modify_time',
                 full_row=False):
        """get a value for an entry in the info table,
        if this key doesn't exist, it will be added with the default
        value and the default value will be returned.

        use as_int, as_bool and full_row to alter the output.
        """
        errmsg = "get_info expected 1 or None value for name='%s'"
        cls, table = self.get_table('info')
        q = self.query(table)
        if orderby is not None and hasattr(cls, orderby):
            q = q.order_by(getattr(cls, orderby))

        if prefix is not None:
            return q.filter(cls.key.startswith(prefix)).all()

        if key is None:
            return q.all()

        vals = q.filter(cls.key==key).all()
        thisrow = None_or_one(vals, errmsg % key)
        if thisrow is None:
            out = default
            data = {'key': key, 'value': default}
            table.insert().execute(**data)
        else:
            out = thisrow.value

        if as_int:
            if out is None: out = 0
            out = int(float(out))
        elif as_bool:
            if out is None: out = 0
            out = bool(int(out))
        elif full_row:
            out = thisrow
        return out

    def set_config(self, name, text):
        """add configuration, general purpose table"""
        cls, table = self.get_table('config')
        row = self.get_config(name)
        if row is None:
            table.insert().execute(name=name, notes=text)
        else:
            q = table.update().where(table.c.name==name)
            q.values({table.c.notes: text}).execute()

        self.commit()

    def get_config(self, name):
        """get configuration, general purpose table"""
        return self.getrow('config', name, one_or_none=True)

    def get_config_id(self, idnum):
        """get configuration by ID"""
        cls, table = self.get_table('config')
        return self.query(table).filter(cls.id==idnum).one()

    def add_slewscanstatus(self, text):
        """add message to slewscanstatus table"""
        cls, table = self.get_table('slewscanstatus')
        table.insert().execute(text=text)
        self.commit()

    def clear_slewscanstatus(self, **kws):
        cls, table = self.get_table('slewscanstatus')
        a = self.get_slewscanstatus()
        if len(a) < 0:
            return
        self.session.execute(table.delete())
        self.commit()

    def get_slewscanstatus(self, **kws):
        return self.select('slewscanstatus', orderby='id', **kws)

    def read_slewscan_status(self):
        text = []
        for row in self.select('slewscanstatus', orderby='id'):
            text.append(str(row.text))
        return "\n".join(text)

    def last_slewscan_status(self):
        lastrow = self.select('slewscanstatus', orderby='id')[-1]
        return lastrow.modify_time.isoformat()

    def set_message(self, text):
        """add message to messages table"""
        cls, table = self.get_table('messages')
        table.insert().execute(text=text)
        self.commit()

    def set_info(self, key, value, notes=None):
        """set key / value in the info table"""
        cls, table = self.get_table('info')
        vals  = self.query(table).filter(cls.key==key).all()
        data = {'key': key, 'value': value}
        if notes is not None:
            data['notes'] = notes
        if len(vals) < 1:
            table = table.insert()
        else:
            table = table.update(whereclause=text("key='%s'" % key))
        table.execute(**data)
        self.commit()

    def clobber_all_info(self):
        """dangerous!!!! clear all info --
        can leave a DB completely broken and unusable
        useful when going to repopulate db anyway"""
        cls, table = self.get_table('info')
        self.session.execute(table.delete().where(table.c.key!=''))

    def set_hostpid(self, clear=False):
        """set hostname and process ID, as on intial set up"""
        name, pid = '', '0'
        if not clear:
            name, pid = gethostname(), str(os.getpid())
        self.set_info('host_name', name)
        self.set_info('process_id', pid)

    def check_hostpid(self):
        """check whether hostname and process ID match current config"""
        if not self.server.startswith('sqlite'):
            return True
        db_host_name = self.get_info('host_name', default='')
        db_process_id  = self.get_info('process_id', default='0')
        return ((db_host_name == '' and db_process_id == '0') or
                (db_host_name == gethostname() and
                 db_process_id == str(os.getpid())))

    def __addRow(self, table, argnames, argvals, **kws):
        """add generic row"""
        table = table()
        for name, val in zip(argnames, argvals):
            setattr(table, name, val)
        for key, val in kws.items():
            if key == 'attributes':
                val = json_encode(val)
            setattr(table, key, val)
        try:
            self.session.add(table)
        except IntegrityError(msg):
            self.session.rollback()
            raise Warning('Could not add data to table %s\n%s' % (table, msg))

        return table

    def _get_foreign_keyid(self, table, value, name='name',
                           keyid='id', default=None):
        """generalized lookup for foreign key
        arguments
        ---------
           table: a valid table class, as mapped by mapper.
           value: can be one of the following table instance:
              keyid is returned string
        'name' attribute (or set which attribute with 'name' arg)
        a valid id
        """
        if isinstance(value, table):
            return getattr(table, keyid)
        else:
            if isinstance(value, (str, unicode)):
                xfilter = getattr(table, name)
            elif isinstance(value, int):
                xfilter = getattr(table, keyid)
            else:
                return default
            try:
                query = self.query(table).filter(
                    xfilter==value)
                return getattr(query.one(), keyid)
            except (IntegrityError, NoResultFound):
                return default

        return default

    def update_where(self, table, where, vals):
        """update a named table with dicts for 'where' and 'vals'"""
        if table in self.tables:
            table = self.tables[table]
        constraints = ["%s=%s" % (str(k), repr(v)) for k, v in where.items()]
        whereclause = ' AND '.join(constraints)
        table.update(whereclause=text(whereclause)).execute(**vals)
        self.commit()

    def getrow(self, table, name, one_or_none=False):
        """return named row from a table"""
        cls, table = self.get_table(table)
        if table is None: return None
        if isinstance(name, Table):
            return name
        out = self.query(table).filter(cls.name==name).all()
        if one_or_none:
            return None_or_one(out, 'expected 1 or None from table %s' % table)
        return out


    # Scan Definitions
    def get_scandef(self, name):
        """return scandef by name"""
        return self.getrow('scandefs', name, one_or_none=True)

    def rename_scandef(self, scanid, name):
        cls, table = self.get_table('scandefs')
        table.update(whereclause=text("id='%d'" % scanid)).execute(name=name)

    def del_scandef(self, name=None, scanid=None):
        """delete scan defn by name"""
        cls, table = self.get_table('scandefs')
        if name is not None:
            self.session.execute(table.delete().where(table.c.name==name))
        elif scanid is not None:
            self.session.execute(table.delete().where(table.c.id==scanid))

    def add_scandef(self, name, text='', notes='', type='', **kws):
        """add scan"""
        cls, table = self.get_table('scandefs')
        kws.update({'notes': notes, 'text': text, 'type': type})

        name = name.strip()
        row = self.__addRow(cls, ('name',), (name,), **kws)
        self.session.add(row)
        return row

    def get_scandict(self, scanname):
        """return dictionary of scan configuration for a named scan"""
        sobj = self.get_scandef(scanname)
        if sobj is None:
            raise ScanDBException('get_scandict needs valid scan name')
        return json.loads(sobj.text, object_hook=asciikeys)

    def make_scan(self, scanname, filename='scan.001',
                  data_callback=None, larch=None):
        """
        create a StepScan object from a saved scan definition

        Arguments
        ---------
        scanname (string): name of scan
        filename (string): name for datafile

        Returns
        -------
        scan object
        """
        try:
            sdict = self.get_scandict(scanname)
        except ScanDBException:
            raise ScanDBException("make.scan(): '%s' not a valid scan name" % scanname)

        if 'rois' not in sdict:
            sdict['rois'] = json.loads(self.get_info('rois'), object_hook=asciikeys)
        sdict['filename'] = filename
        sdict['scandb'] = self
        sdict['larch'] = larch
        sdict['data_callback'] = data_callback
        sdict['extra_pvs'] = []
        for row  in self.getall('extrapvs', orderby='id'):
            if row.use:
                sdict['extra_pvs'].append((row.name, row.pvname))
        return create_scan(**sdict)

    # macros
    def get_macro(self, name):
        """return macro by name"""
        return self.getrow('macros', name, one_or_none=True)

    def add_macro(self, name, text, arguments='',
                  output='', notes='', **kws):
        """add macro"""
        cls, table = self.get_table('macros')
        name = name.strip()
        kws.update({'notes': notes, 'text': text,
                    'arguments': arguments})
        row = self.__addRow(cls, ('name',), (name,), **kws)
        self.session.add(row)
        return row

    ## scan data
    ## note that this is supported differently for Postgres and Sqlite:
    ##    With Postgres, data arrays are held internally,
    ##    With Sqlite, data is held as json-ified arrays
    def get_scandata(self, **kws):
        return self.select('scandata', orderby='id', **kws)

    def add_scandata(self, name, value, notes='', pvname='', **kws):
        cls, table = self.get_table('scandata')
        name = name.strip()
        kws.update({'notes': notes, 'pvname': pvname})
        if self.server.startswith('sqli'):
            value = json_encode(value)
        row = self.__addRow(cls, ('name', 'data'), (name, value), **kws)
        self.session.add(row)
        self.commit()
        return row

    def set_scandata(self, name, value,  **kws):
        cls, tab = self.get_table('scandata')
        if isinstance(value, np.ndarray):
            value = value.tolist()
        elif isinstance(value, tuple):
            val = list(value)
        if isinstance(value, (int, float)):
            value = [value]
        where = "name='%s'" % name
        update = tab.update().where(whereclause=text(where))
        if self.server.startswith('sqli'):
            update.execute(data=json_encode(value))
        else:
            update.values({tab.c.data: value}).execute()
        # self.commit()

    def append_scandata(self, name, val):
        cls, tab = self.get_table('scandata')
        where = "name='%s'" % name
        tselect = tab.select(whereclause=text(where))
        tupdate = tab.update().where(whereclause=text(where))
        if self.server.startswith('sqli'):
            data = json.loads(tselect.execute().fetchone().data)
            data.append(val)
            tupdate.execute(data=json_encode(data))
        else:
            n = len(tselect.execute().fetchone().data)
            tupdate.values({tab.c.data[n]: val}).execute()
        self.commit()

    def clear_scandata(self, **kws):
        cls, table = self.get_table('scandata')
        a = self.get_scandata()
        if len(a) < 0:
            return
        self.session.execute(table.delete().where(table.c.id != 0))
        self.commit()

    ### positioners
    def get_positioners(self, **kws):
        return self.getall('scanpositioners', orderby='id', **kws)

    def get_slewpositioners(self, **kws):
        return self.getall('slewscanpositioners', orderby='id', **kws)


    def get_positioner(self, name):
        """return positioner by name"""
        return self.getrow('scanpositioners', name, one_or_none=True)

    def del_slewpositioner(self, name):
        """delete slewscan positioner by name"""
        cls, table = self.get_table('slewscanpositioners')
        self.session.execute(table.delete().where(table.c.name==name))

    def del_positioner(self, name):
        """delete positioner by name"""
        cls, table = self.get_table('scanpositioners')

        self.session.execute(table.delete().where(table.c.name==name))

    def add_positioner(self, name, drivepv, readpv=None, notes='',
                       extrapvs=None, **kws):
        """add positioner"""
        cls, table = self.get_table('scanpositioners')
        name = name.strip()
        drivepv = pv_fullname(drivepv)
        if readpv is not None:
            readpv = pv_fullname(readpv)
        epvlist = []
        if extrapvs is not None:
            epvlist = [pv_fullname(p) for p in extrapvs]
        kws.update({'notes': notes, 'drivepv': drivepv,
                    'readpv': readpv, 'extrapvs':json.dumps(epvlist)})

        row = self.__addRow(cls, ('name',), (name,), **kws)
        self.session.add(row)
        self.add_pv(drivepv, notes=name)
        if readpv is not None:
            self.add_pv(readpv, notes="%s readback" % name)
        for epv in epvlist:
            self.add_pv(epv)
        return row

    def get_slewpositioner(self, name):
        """return slewscan positioner by name"""
        return self.getrow('slewscanpositioners', name, one_or_none=True)

    def add_slewpositioner(self, name, drivepv, readpv=None, notes='',
                           extrapvs=None, **kws):
        """add slewscan positioner"""
        cls, table = self.get_table('slewscanpositioners')
        name = name.strip()
        drivepv = pv_fullname(drivepv)
        if readpv is not None:
            readpv = pv_fullname(readpv)
        epvlist = []
        if extrapvs is not None:
            epvlist = [pv_fullname(p) for p in extrapvs]
        kws.update({'notes': notes, 'drivepv': drivepv,
                    'readpv': readpv, 'extrapvs':json.dumps(evpvlist)})

        row = self.__addRow(cls, ('name',), (name,), **kws)
        self.session.add(row)
        self.add_pv(drivepv, notes=name)
        if readpv is not None:
            self.add_pv(readpv, notes="%s readback" % name)
        for epv in epvlist:
            self.add_pv(epv)
        return row

    ### detectors
    def get_detectors(self, **kws):
        return self.getall('scandetectors', orderby='id', **kws)

    def get_detector(self, name):
        """return detector by name"""
        return self.getrow('scandetectors', name, one_or_none=True)

    def del_detector(self, name):
        """delete detector by name"""
        cls, table = self.get_table('scandetectors')
        self.session.execute(table.delete().where(table.c.name==name))

    def add_detector(self, name, pvname, kind='', options='', **kws):
        """add detector"""
        cls, table = self.get_table('scandetectors')
        name = name.strip()
        pvname = pv_fullname(pvname)
        kws.update({'pvname': pvname,
                    'kind': kind, 'options': options})
        row = self.__addRow(cls, ('name',), (name,), **kws)
        self.session.add(row)
        return row

    ### detector configurations
    def get_detectorconfigs(self, **kws):
        return self.getall('scandetectorconfig', orderby='id', **kws)

    def get_detectorconfig(self, name, **kws):
        return self.getrow('scandetectorconfig', name, one_or_none=True)

    def set_detectorconfig(self, name, text, notes=None):
        """set detector configuration"""
        cls, table = self.get_table('scandetectorconfig')
        row = self.get_detectorconfig(name)
        if row is None:
            args = dict(name=name, text=text)
            if notes is not None:
                args['notes'] = notes
            table.insert().execute(**args)
        else:
            q = table.update().where(table.c.name==name)
            q = q.values({table.c.text: text})
            if notes is not None:
                q = q.values({table.c.notes: notes})
            q.execute()

        self.commit()

    ### counters -- simple, non-triggered PVs to add to detectors
    def get_counters(self, **kws):
        return self.getall('scancounters', orderby='id', **kws)

    def get_counter(self, name):
        """return counter by name"""
        return self.getrow('scancounters', name, one_or_none=True)

    def del_counter(self, name):
        """delete counter by name"""
        cls, table = self.get_table('scancounters')
        self.session.execute(table.delete().where(table.c.name==name))

    def add_counter(self, name, pvname, **kws):
        """add counter (non-triggered detector)"""
        cls, table = self.get_table('scancounters')
        pvname = pv_fullname(pvname)
        name = name.strip()
        kws.update({'pvname': pvname})
        row = self.__addRow(cls, ('name',), (name,), **kws)
        self.session.add(row)
        self.add_pv(pvname, notes=name)
        return row

    ### extra pvs: pvs recorded at breakpoints of scans
    def get_extrapvs(self, **kws):
        return self.getall('extrapvs', orderby='id', **kws)

    def get_extrapv(self, name):
        """return extrapv by name"""
        return self.getrow('extrapvs', name, one_or_none=True)

    def del_extrapv(self, name):
        """delete extrapv by name"""
        cls, table = self.get_table('extrapvs')
        self.session.execute(table.delete().where(table.c.name==name))

    def add_extrapv(self, name, pvname, use=True, **kws):
        """add extra pv (recorded at breakpoints in scans"""
        cls, table = self.get_table('extrapvs')
        name = name.strip()
        pvname = pv_fullname(pvname)
        kws.update({'pvname': pvname, 'use': int(use)})
        row = self.__addRow(cls, ('name',), (name,), **kws)
        self.session.add(row)
        self.add_pv(pvname, notes=name)
        return row

    def get_common_commands(self):
        return self.getall('common_commands', orderby='display_order')

    def add_common_commands(self, name, args='', show=True, display_order=1000):
        """add extra pv (recorded at breakpoints in scans"""
        cls, table = self.get_table('common_commands')
        name = name.strip()
        kws = dict(args=args.strip(), show=int(show), display_order=int(display_order))
        row = self.__addRow(cls, ('name',), (name,), **kws)
        self.session.add(row)
        return row


    # add PV to list of PVs
    def add_pv(self, name, notes='', monitor=False):
        """add pv to PV table if not already there """
        if len(name) < 2:
            return
        name = pv_fullname(name)
        cls, table = self.get_table('pv')
        vals  = self.query(table).filter(table.c.name == name).all()
        ismon = {False:0, True:1}[monitor]
        if len(vals) < 1:
            table.insert().execute(name=name, notes=notes, is_monitor=ismon)
        elif notes is not '':
            where = "name='%s'" % name
            table.update(whereclause=text(where)).execute(notes=notes,
                                                    is_monitor=ismon)
        thispv = self.query(table).filter(cls.name == name).one()
        self.connect_pvs(names=[name])
        return thispv

    def get_pvrow(self, name):
        """return db row for a PV"""
        if len(name) < 2:
            return
        cls, table = self.get_table('pv')
        out = table.select().where(table.c.name == name).execute().fetchall()
        return None_or_one(out, 'get_pvrow expected 1 or None PV')

    def get_pv(self, name):
        """return pv object from known PVs"""
        if len(name) > 2:
            name = pv_fullname(name)
            if name in self.pvs:
                return self.pvs[name]

    def connect_pvs(self, names=None):
        "connect all PVs in pvs table"
        if names is None:
            cls, table = self.get_table('pv')
            names = [str(row.name) for row in self.query(table).all()]

        _connect = []
        for name in names:
            name = pv_fullname(name)
            if len(name) < 2:
                continue
            if name not in self.pvs:
                self.pvs[name] = epics.PV(name)
                _connect.append(name)

        for name in _connect:
            connected, count = False, 0
            while not connected:
                time.sleep(0.001)
                count += 1
                connected = self.pvs[name].connected or count > 100

    def record_monitorpv(self, pvname, value):
        """save value for monitor pvs
        pvname = pv_fullname(pvname)
        if pvname not in self.pvs:
            pv = self.add_pv(pvname, monitor=True)


        #cls, table = self.get_table('monitorvalues')
        #mval = cls()
        ## mval.pv_id = self.pvs[pvname]
        #mval.value = value
        #self.session.add(mval)
        """
        pass

    def get_monitorvalues(self, pvname, start_date=None, end_date=None):
        """get (value, time) pairs for a monitorpvs given a time range

        pvname = pv_fullname(pvname)
        if pvname not in self.pvs:
            pv = self.add_monitorpv(pvname)
            # self.pvs[pvname] = pv.id

        cls, valtab = self.get_table('monitorvalues')

        query = select([valtab.c.value, valtab.c.time],
                       valtab.c.monitorpvs_id==self.pvs[pvname])
        if start_date is not None:
            query = query.where(valtab.c.time >= start_date)
        if end_date is not None:
            query = query.where(valtab.c.time <= end_date)

        return query.execute().fetchall()
        """
        pass

    ### commands -- a more complex interface
    def get_commands(self, status=None, reverse=False, orderby='run_order',
                     requested_since=None, **kws):
        """return command by status"""
        cls, table = self.get_table('commands')
        order = cls.id
        if orderby.lower().startswith('run'):
            order = cls.run_order
        if reverse:
            order = order.desc()

        q = table.select().order_by(order)
        if status in self.status_codes:
            q = q.where(table.c.status_id == self.status_codes[status])
        if requested_since is not None:
            q = q.where(table.c.request_time >= requested_since)
        return q.execute().fetchall()

    # commands -- a more complex interface
    def get_mostrecent_command(self):
        """return last command entered"""
        cls, table = self.get_table('commands')
        q = self.query(cls).order_by(cls.request_time)
        return q.all()[-1]


    def add_command(self, command, arguments='',output_value='',
                    output_file='', notes='', nrepeat=1, **kws):
        """add command"""
        cls, table = self.get_table('commands')
        statid = self.status_codes.get('requested', 1)

        kws.update({'arguments': arguments,
                    'output_file': output_file,
                    'output_value': output_value,
                    'notes': notes,
                    'nrepeat': nrepeat,
                    'status_id': statid})

        this  = cls()
        this.command = command
        for key, val in kws.items():
            if key == 'attributes':
                val = json_encode(val)
            setattr(this, key, val)

        self.session.add(this)
        return this

    def get_current_command_id(self):
        """return id of current command"""
        cmdid  = self.get_info('current_command_id', default=0)
        if cmdid == 0:
            cmdid = self.get_mostrecent_command().id
        return int(cmdid)

    def get_current_command(self):
        """return command by status"""
        cmdid  = self.get_current_command_id()
        cls, table = self.get_table('commands')
        q = table.select().where(table.c.id==cmdid)
        return q.execute().fetchall()[0]

    def get_command_status(self, cmdid=None):
        "get status for a command by id"
        if cmdid is None:
            cmdid = self.get_current_command_id()
        cls, table = self.get_table('commands')
        ret = table.select().where(table.c.id==cmdid).execute().fetchone()
        return self.status_names[ret.status_id]

    def set_command_status(self, status, cmdid=None):
        """set the status of a command (by id)"""
        if cmdid is None:
            cmdid = self.get_current_command_id()

        cls, table = self.get_table('commands')
        status = status.lower()
        if status not in self.status_codes:
            status = 'unknown'

        statid = self.status_codes[status]
        thiscmd = table.update(whereclause=text("id='%i'" % cmdid))
        thiscmd.execute(status_id=statid)
        if status.startswith('start'):
            thiscmd.execute(start_time=datetime.now())

    def set_command_run_order(self, run_order, cmdid):
        """set the run_order of a command (by id)"""
        cls, table = self.get_table('commands')
        table.update(whereclause=text("id='%i'" % cmdid)).execute(run_order=run_order)

    def set_filename(self, filename):
        """set filename for info and command"""
        self.set_info('filename', filename)
        ufolder = self.get_info('user_folder', default='')
        self.set_command_filename(os.path.join(ufolder, filename))

    def set_command_filename(self, filename, cmdid=None):
        """set filename for command"""
        if cmdid is None:
            cmdid  = self.get_current_command_id()
        cls, table = self.get_table('commands')
        table.update(whereclause=text("id='%i'" % cmdid)).execute(output_file=filename)

    def set_command_output(self, value=None, cmdid=None):
        """set the status of a command (by id)"""
        if cmdid is None:
            cmdid  = self.get_current_command_id()
        cls, table = self.get_table('commands')
        table.update(whereclause=text("id='%i'" % cmdid)).execute(output_value=repr(value))

    def replace_command(self, cmdid, new_command):
        """replace requested  command"""
        cls, table = self.get_table('commands')
        row = table.select().where(table.c.id==cmdid).execute().fetchone()
        if self.status_names[row.status_id].lower() == 'requested':
            table.update(whereclause=text("id='%i'" % cmdid)).execute(command=new_command)
        

    def cancel_command(self, cmdid):
        """cancel command"""
        self.set_command_status('canceled', cmdid)
        cls, table = self.get_table('commands')
        canceled  = self.status_codes['canceled']
        table.update(whereclause=text("id='%d'" % cmdid)
        ).execute(status_id=canceled)


    def cancel_remaining_commands(self):
        """cancel all commmands to date"""
        cls, table = self.get_table('commands')
        requested = self.status_codes['requested']
        canceled  = self.status_codes['canceled']
        for r in table.select().where(table.c.status_id==requested
                                      ).order_by(cls.run_order
                                      ).execute().fetchall():
            table.update(whereclause=text("id='%d'" % r.id)
            ).execute(status_id=canceled)

    def test_abort(self, msg='scan abort'):
        """look for abort, raise ScanDBAbort if set"""
        return self.get_info('request_abort', as_bool=True)

    def wait_for_pause(self, timeout=86400.0):
        """if request_pause is set, wait until it is unset"""
        paused = self.get_info('request_pause', as_bool=True)
        if not paused:
            return

        t0 = time.time()
        while paused:
            time.sleep(0.25)
            paused = (self.get_info('request_pause', as_bool=True) and
                      (time.time() - t0) < timeout)

class InstrumentDB(object):
    """Instrument / Position class using a scandb instance"""

    def __init__(self, scandb):
        self.scandb = scandb

    def __addRow(self, table, argnames, argvals, **kws):
        """add generic row"""
        table = table()
        for name, val in zip(argnames, argvals):
            setattr(table, name, val)
        for key, val in kws.items():
            if key == 'attributes':
                val = json_encode(val)
            setattr(table, key, val)
        try:
            self.scandb.session.add(table)
        except IntegrityError(msg):
            self.scandb.session.rollback()
            raise Warning('Could not add data to table %s\n%s' % (table, msg))


    ### Instrument Functions
    def add_instrument(self, name, pvs=None, notes=None, attributes=None, **kws):
        """add instrument
        notes and attributes optional
        returns Instruments instance"""
        kws['notes'] = notes
        kws['attributes'] = attributes
        name = name.strip()
        inst = self.get_instrument(name)
        if inst is None:
            out = self.__addRow(Instrument, ('name',), (name,), **kws)
            inst = self.get_instrument(name)
        cls, jointable = self.scandb.get_table('instrument_pv')
        if pvs is not None:
            pvlist = []
            for pvname in pvs:
                thispv = self.scandb.get_pvrow(pvname)
                if thispv is None:
                    thispv = self.scandb.add_pv(pvname)
                pvlist.append(thispv)
            for dorder, pv in enumerate(pvlist):
                data = {'display_order': dorder, 'pv_id': pv.id,
                        'instrument_id': inst.id}
                jointable.insert().execute(**data)

        self.scandb.session.add(inst)
        self.scandb.commit()
        return inst

    def get_all_instruments(self):
        """return instrument list
        """
        cls, table = self.scandb.get_table('instrument')
        return self.scandb.query(cls).order_by(cls.display_order).all()

    def get_instrument(self, name):
        """return instrument by name
        """
        if isinstance(name, Instrument):
            return name
        cls, table = self.scandb.get_table('instrument')
        out = self.scandb.query(cls).filter(cls.name==name).all()
        return None_or_one(out, 'get_instrument expected 1 or None Instruments')

    def remove_position(self, instname, posname):
        inst = self.get_instrument(instname)
        if inst is None:
            raise ScanDBException('remove_position needs valid instrument')

        posname = posname.strip()
        pos  = self.get_position(instname, posname)
        if pos is None:
            raise ScanDBException("Postion '%s' not found for '%s'" %
                                        (posname, inst.name))

        cls, tab = self.scandb.get_table('position_pv')
        self.scandb.conn.execute(tab.delete().where(tab.c.position_id==pos.id))
        self.scandb.conn.execute(tab.delete().where(tab.c.position_id==None))

        cls, ptab = self.scandb.get_table('position')
        self.scandb.conn.execute(ptab.delete().where(ptab.c.id==pos.id))
        self.scandb.commit()

    def remove_all_positions(self, instname):
        for posname in self.get_positionlist(instname):
            self.remove_position(instname, posname)

    def remove_instrument(self, inst):
        inst = self.get_instrument(inst)
        if inst is None:
            raise ScanDBException('Save Postion needs valid instrument')

        tab = self.scandb.tables['instrument']
        self.scandb.conn.execute(tab.delete().where(tab.c.id==inst.id))

        for tablename in ('position', 'instrument_pv', 'instrument_precommand',
                          'instrument_postcommand'):
            tab = self.scandb.tables[tablename]
            self.scandb.conn.execute(tab.delete().where(tab.c.instrument.id==inst.id))

    def save_position(self, instname, posname, values, image=None, notes=None, **kw):
        """save position for instrument
        """
        inst = self.get_instrument(instname)
        if inst is None:
            raise ScanDBException('Save Postion needs valid instrument')

        posname = posname.strip()
        pos  = self.get_position(instname, posname)
        pos_cls, pos_table = self.scandb.get_table('position')
        _, ppos_tab = self.scandb.get_table('position_pv')
        _, pvs_tab  = self.scandb.get_table('pv')
        _, ipv_tab  = self.scandb.get_table('instrument_pv')

        if pos is None:
            pos = pos_cls()
            pos.name = posname
            pos.instrument_id = inst.id

        pos.modify_time = datetime.now()
        if image is not None:
            pos.image = image
        if notes is not None:
            pos.notes = notes
        self.scandb.session.add(pos)
        pos  = self.get_position(instname, posname)
        ## print("  Position: ", pos, pos.id)

        pvnames = []
        for pvs in ipv_tab.select().where(ipv_tab.c.instrument_id==inst.id).execute().fetchall():
            name = pvs_tab.select().where(pvs_tab.c.id==pvs.pv_id).execute().fetchone().name
            pvnames.append(str(name))

        ## print("@ Save Position: ", posname, pvnames, values)
        # check for missing pvs in values
        missing_pvs = []
        for pv in pvnames:
            if pv not in values:
                missing_pvs.append(pv)

        if len(missing_pvs) > 0:
            raise ScanDBException('save_position: missing pvs:\n %s' %
                                        missing_pvs)

        doexec = self.scandb.conn.execute
        doexec(ppos_tab.delete().where(ppos_tab.c.position_id == None))
        doexec(ppos_tab.delete().where(ppos_tab.c.position_id == pos.id))

        pos_pvs = []
        for name in pvnames:
            thispv = self.scandb.get_pvrow(name)
            val = values[name]
            if val is not None:
                try:
                    val = float(val)
                except:
                    pass
                ppos_tab.insert().execute(pv_id=thispv.id,
                                          position_id = pos.id,
                                          notes= "'%s' / '%s'" % (inst.name, posname),
                                          value = val)
        self.scandb.commit()


    def save_current_position(self, instname, posname, image=None, notes=None):
        """save current values for an instrument to posname
        """
        inst = self.get_instrument(instname)
        if inst is None:
            raise ScanDBException('Save Postion needs valid instrument')
        vals = {}
        for pv in inst.pvs:
            vals[pv.name] = epics.caget(pv.name)
        self.save_position(instname, posname,  vals, image=image, notes=notes)

    def restore_complete(self):
        "return whether last restore_position has completed"
        if len(self.restoring_pvs) > 0:
            return all([p.put_complete for p in self.restoring_pvs])
        return True

    def rename_position(self, inst, oldname, newname):
        """rename a position"""
        pos = self.get_position(inst, oldname)
        if pos is not None:
            pos.name = newname
            self.scandb.commit()

    def get_position(self, instname, posname):
        """return position from namea and instrument
        """
        inst = self.get_instrument(instname)
        cls, table = self.scandb.get_table('position')
        filter = and_(cls.name==posname, cls.instrument_id==inst.id)
        out = self.scandb.query(cls).filter(filter).all()
        return None_or_one(out, 'get_position expected 1 or None Position')

    def get_position_vals(self, instname, posname):
        """return position with dictionary of PVName:Value pairs"""
        pos = self.get_position(instname, posname)
        pv_vals = {}
        _c, ppos_tab = self.scandb.get_table('position_pv')
        _c, pv_tab   = self.scandb.get_table('pv')
        pvnames = dict([(pv.id, str(pv.name)) for pv in pv_tab.select().execute().fetchall()])

        for pvval in ppos_tab.select().where(ppos_tab.c.position_id == pos.id).execute().fetchall():
            pv_vals[ pvnames[pvval.pv_id]]= float(pvval.value)
        return pv_vals

    def get_positionlist(self, instname, reverse=False):
        """return list of position names for an instrument
        """
        inst = self.get_instrument(instname)
        cls, table = self.scandb.get_table('position')
        q = self.scandb.query(cls)
        q = q.filter(cls.instrument_id==inst.id)
        q = q.order_by(cls.modify_time)
        out = [p.name for p in q.all()]
        if reverse:
            out.reverse()
        return out

    def restore_position(self, instname, posname, wait=False, timeout=5.0,
                         exclude_pvs=None):
        """
        restore named position for instrument
        """
        inst = self.get_instrument(instname)
        if inst is None:
            raise ScanDBException('restore_postion needs valid instrument')

        posname = posname.strip()
        pos  = self.get_position(instname, posname)

        if pos is None:
            raise ScanDBException(
                "restore_postion  position '%s' not found" % posname)

        if exclude_pvs is None:
            exclude_pvs = []

        pv_vals = []
        ppos_cls, ppos_tab = self.scandb.get_table('position_pv')
        pv_cls, pv_tab     = self.scandb.get_table('pv')
        pvnames = dict([(pv.id, str(pv.name)) for pv in pv_tab.select().execute().fetchall()])

        pv_vals = []
        for pvval in ppos_tab.select().where(ppos_tab.c.position_id == pos.id).execute().fetchall():
            pvname = pvnames[pvval.pv_id]
            if pvname not in exclude_pvs:
                val = pvval.value
                try:
                    val = float(pvval.value)
                except ValueError:
                    pass
                pv_vals.append((epics.get_pv(pvname), val))

        epics.ca.poll()
        # put values without waiting
        for thispv, val in pv_vals:
            if not thispv.connected:
                thispv.wait_for_connection(timeout=timeout)
            try:
                thispv.put(val)
            except:
                pass

        if wait:
            for thispv, val in pv_vals:
                try:
                    thispv.put(val, wait=True)
                except:
                    pass
