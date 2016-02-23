#!/usr/bin/env python

import time, sys, os
import json
import numpy as np
import glob
from collections import OrderedDict

from .file_utils import nativepath
from .site_config import get_fileroot, LARCH_SCANDB, LARCH_INSTDB
from .utils import plain_ascii
from .scandb import InstrumentDB
from . import scandb

import epics

# larch_site_config is not used here, but is imported from here
class Empty:
    pass
larch_site_config = Empty()
larch_site_config.larchdir = ''

HAS_LARCH = False
try:
    import larch
    larch_site_config = larch.site_config
    HAS_LARCH = True
except:
    pass


class LarchScanDBWriter(object):
    """Writer for Larch Interface that writes to both Stdout
    and Messages table of scandb
    """
    def __init__(self, stdout=None, scandb=None, _larch=None):
        if stdout is None:
            stdout = sys.stdout
        self.scandb = scandb
        self.writer = stdout
        self._larch = _larch

    def write(self, text, color=None, bkg=None, **kws):
        """write text to writer
        write('hello', color='red', bkg='grey', bold=True, blink=True)
        """
        attrs = []
        self.writer.write(text)
        if self.scandb is not None:
            self.scandb.set_message(text)

    def flush(self):
        self.writer.flush()
        if self.scandb is not None:
            self.scandb.commit()

                
class LarchScanDBServer(object):
    """      """
    def __init__(self, scandb, fileroot=None):
        self.scandb = scandb
        self.fileroot = get_fileroot(fileroot)
        self.writer = LarchScanDBWriter(scandb=scandb)
        self.macro_dir = self.scandb.get_info('macro_folder')
        self.loaded_modules = {}


        self.larch = self.symtab = None
        if HAS_LARCH:
            self.larch  = larch.Interpreter(writer=self.writer)
            self.symtab = self.larch.symtable
            self.symtab.set_symbol(LARCH_SCANDB, self.scandb)
            self.symtab.set_symbol(LARCH_INSTDB, InstrumentDB(self.scandb))
            self.symtab._sys.color_exceptions = False
            self.enable_abort()

    def check_abort_pause(self, msg='at caget'):
        self.scandb.test_abort(msg)
        self.scandb.wait_for_pause(timeout=86400.0)

    def enable_abort(self):
        """this replaes several larch functions with
        functions that support raising ScanDBAbort exceptions
        """
        def caget(pvname, _larch=None, **kws):
            amsg = "at caget('%s')" % pvname
            self.check_abort_pause(msg=amsg)
            return epics.caget(pvname, **kws)

        def caput(pvname, value, _larch=None, **kws):
            amsg = "at caput('%s', %s)" % (pvname, repr(value))
            self.check_abort_pause(msg=amsg)
            return epics.caput(pvname, value, **kws)

        def PV(pvname, _larch=None, **kws):
            amsg = "at PV('%s')" % pvname
            self.check_abort_pause(msg=amsg)
            return epics.get_pv(pvname, **kws)

        self.symtab.set_symbol('_epics.caget', caget)
        self.symtab.set_symbol('_epics.caput', caput)
        self.symtab.set_symbol('_epics.PV', PV)

    def load_plugins(self, macro_dir=None):
        print("load_plugins() no longer needed")
        return
        if not HAS_LARCH:
            return
        if macro_dir is None:
            macro_dir = self.macro_dir
        if macro_dir is None:
            print("load_plugins: no Macro folder")
            return
        else:
            plugindir = os.path.join(self.fileroot, macro_dir, 'plugins')
            self.symtab._sys.config.plugins_path.insert(0, plugindir)
            for pyfile in glob.glob(os.path.join(plugindir, '*.py')):
                plugin_name = str(os.path.split(pyfile)[1][:-3])
                out = self.larch.run("add_plugin('%s')" % plugin_name)
                if  not out:
                    print("Error adding plugin '%s'" % (plugin_name))
                    if len(self.larch.error) > 0:
                        emsg = '\n'.join(self.larch.error[0].get_error())
                        self.scandb.set_info('error_message', emsg)

    def load_modules(self, macro_dir=None, verbose=False):
        self.load_macros(macro_dir=macro_dir, verbose=verbose)
        
    def load_macros(self, macro_dir=None, verbose=False):        
        """read latest larch macros / modules"""
        if not HAS_LARCH:
            return

        if macro_dir is None:
            macro_dir = self.macro_dir

        moduledir = os.path.join(self.fileroot, macro_dir, 'macros')
        _sys = self.symtab._sys
        if moduledir not in _sys.path:
            _sys.path.insert(0, moduledir)
        if not os.path.exists(moduledir):
            self.scandb.set_info('scan_message',
                                 "Cannot locate modules in '%s'" % moduledir)
            return

        try:
            origdir = os.getcwd()
            os.chdir(moduledir)
            for name in glob.glob('*.lar'):
                time.sleep(0.025)
                modname = name[:-4]
                this_mtime = os.stat(name).st_mtime
                if modname in self.loaded_modules:
                    last_mtime = self.loaded_modules[modname]
                    if this_mtime < last_mtime:
                        continue

                self.larch.error = []
                if verbose:
                    print 'importing module: ', modname
                if modname in self.loaded_modules:
                    self.larch.run('reload(%s)' % modname)
                else:
                    self.larch.run('import %s' % modname)
                if len(self.larch.error) > 0:
                    emsg = '\n'.join(self.larch.error[0].get_error())
                    self.scandb.set_info('error_message', emsg)
                    print '==Import Error %s/%s' % (modname, emsg)
                else:
                    if modname not in _sys.searchGroups:
                        _sys.searchGroups.append(modname)
                    self.loaded_modules[modname] = this_mtime
                    thismod  = self.symtab.get_symbol(modname)
                    _sys.searchGroupObjects.append(thismod)
            os.chdir(origdir)
        except OSError: 
            pass
        self.scandb.set_path(fileroot=self.fileroot)
        return self.get_macros()
    
    def __call__(self, arg):
        return self.run(arg)

    def run(self, command=None):
        if not HAS_LARCH:
            print("No Larch to run command ", command)
            return
        self.larch.error = []
        if command is None:
            return
        return self.larch.eval(plain_ascii(command))

    def set_symbol(self, name, value):
        self.symtab.set_symbol(name, value)

    def get_symbol(self, name):
        return getattr(self.symtab, name)

    def get_error(self):
        return self.larch.error

    def get_macros(self):
        """return an orderded dictionary of larch functions/procedures
        that are exposed to user for Scan macros

        These are taken from the _epics, _scan, and macros groups

        returned dictionary has function names as keys, and docstrings as values
        """
        macros = OrderedDict()
        symtab = self.symtab
        modlist = [symtab, symtab._epics, symtab._scan]
        for mod in self.loaded_modules:
            if hasattr(symtab, mod):
                modlist.append(getattr(symtab, mod))
        
        for group in modlist:
            for name in dir(group):
                obj = getattr(group, name)
                if callable(obj) and not name.startswith('_'):
                    doc  = obj.__doc__
                    if doc is None:
                        doc = ''
                        if hasattr(obj, '_signature'):
                            doc = obj._signature()
                    if 'PRIVATE' not in doc:
                        macros[name] = doc
        return macros
