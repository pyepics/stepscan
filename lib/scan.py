#!/usr/bin/env python
from __future__ import print_function

MODDOC = """
=== Epics Scanning ===


This does not used the Epics SScan Record, and the scan is intended to run
as a python application, but many concepts from the Epics SScan Record are
borrowed.  Where appropriate, the difference will be noted here.

A Step Scan consists of the following objects:
   a list of Positioners
   a list of Triggers
   a list of Counters

Each Positioner will have a list (or numpy array) of position values
corresponding to the steps in the scan.  As there is a fixed number of
steps in the scan, the position list for each positioners must have the
same length -- the number of points in the scan.  Note that, unlike the
SScan Record, the list of points (not start, stop, step, npts) must be
given.  Also note that the number of positioners or number of points is not
limited.

A Trigger is simply an Epics PV that will start a particular detector,
usually by having 1 written to its field.  It is assumed that when the
Epics ca.put() to the trigger completes, the Counters associated with the
triggered detector will be ready to read.

A Counter is simple a PV whose value should be recorded at every step in
the scan.  Any PV can be a Counter, including waveform records.  For many
detector types, it is possible to build a specialized class that creates
many counters.

Because Triggers and Counters are closely associated with detectors, a
Detector is also defined, which simply contains a single Trigger and a list
of Counters, and will cover most real use cases.

In addition to the core components (Positioners, Triggers, Counters, Detectors),
a Step Scan contains the following objects:

   breakpoints   a list of scan indices at which to pause and write data
                 collected so far to disk.
   extra_pvs     a list of (description, PV) tuples that are recorded at
                 the beginning of scan, and at each breakpoint, to be
                 recorded to disk file as metadata.
   pre_scan()    method to run prior to scan.
   post_scan()   method to run after scan.
   at_break()    method to run at each breakpoint.

Note that Postioners and Detectors may add their own pieces into extra_pvs,
pre_scan(), post_scan(), and at_break().

With these concepts, a Step Scan ends up being a fairly simple loop, going
roughly (that is, skipping error checking) as:

   pos = <DEFINE POSITIONER LIST>
   det = <DEFINE DETECTOR LIST>
   run_pre_scan(pos, det)
   [p.move_to_start() for p in pos]
   record_extra_pvs(pos, det)
   for i in range(len(pos[0].array)):
       [p.move_to_pos(i) for p in pos]
       while not all([p.done for p in pos]):
           time.sleep(0.001)
       [trig.start() for trig in det.triggers]
       while not all([trig.done for trig in det.triggers]):
           time.sleep(0.001)
       [det.read() for det in det.counters]

       if i in breakpoints:
           write_data(pos, det)
           record_exrta_pvs(pos, det)
           run_at_break(pos, det)
   write_data(pos, det)
   run_post_scan(pos, det)

Note that multi-dimensional mesh scans over a rectangular grid is not
explicitly supported, but these can be easily emulated with the more
flexible mechanism of unlimited list of positions and breakpoints.
Non-mesh scans are also possible.

A step scan can have an Epics SScan Record or StepScan database associated
with it.  It will use these for PVs to post data at each point of the scan.
"""
import os
import sys
import shutil
import time
from threading import Thread
import json
import numpy as np
import random
import six

from datetime import timedelta

from file_utils import fix_varname

from epics import PV, poll, get_pv, caget, caput

from .utils import ScanDBException, ScanDBAbort
from .detectors import (Counter, Trigger, AreaDetector)
from .datafile import ASCIIScanFile
from .positioner import Positioner
from .xps import NewportXPS

from .debugtime import debugtime

MIN_POLL_TIME = 1.e-3


def hms(secs):
    "format time in seconds to H:M:S"
    return str(timedelta(seconds=int(secs)))

class ScanMessenger(Thread):
    """ Provides a way to run user-supplied functions per scan point,
    in a separate thread, so as to not delay scan operation.

    Initialize a ScanMessenger with a function to call per point, and the
    StepScan instance.  On .start(), a separate thread will createrd and
    the .run() method run.  Here, this runs a loop, looking at the .cpt
    attribute.  When this .cpt changes, the executing will run the user
    supplied code with arguments of 'scan=scan instance', and 'cpt=cpt'

    Thus, at each point in the scan the scanning process should set .cpt,
    and the user-supplied func will execute.

    To stop the thread, set .cpt to None.  The thread will also automatically
    stop if .cpt has not changed in more than 1 hour
    """
    # number of seconds to wait for .cpt to change before exiting thread
    timeout = 3600.
    def __init__(self, func=None, scan=None,
                 cpt=-1, npts=None, func_kws=None):
        Thread.__init__(self)
        self.func = func
        self.scan = scan
        self.cpt = cpt
        self.npts = npts
        if func_kws is None:
            func_kws = {}
        self.func_kws = func_kws
        self.func_kws['npts'] = npts

    def run(self):
        """execute thread, watching the .cpt attribute. Any chnage will
        cause self.func(cpt=self.cpt, scan=self.scan) to be run.
        The thread will stop when .pt == None or has not changed in
        a time  > .timeout
        """
        last_point = self.cpt
        t0 = time.time()

        while True:
            poll(MIN_POLL_TIME, 0.25)
            if self.cpt != last_point:
                last_point =  self.cpt
                t0 = time.time()
                if self.cpt is not None and hasattr(self.func, '__call__'):
                    self.func(cpt=self.cpt, scan=self.scan,
                              **self.func_kws)
            if self.cpt is None or time.time()-t0 > self.timeout:
                return

class StepScan(object):
    """
    General Step Scanning for Epics
    """
    def __init__(self, filename=None, auto_increment=True,
                 comments=None, messenger=None, scandb=None,
                 prescan_func=None, **kws):
        self.pos_settle_time = MIN_POLL_TIME
        self.det_settle_time = MIN_POLL_TIME
        self.pos_maxmove_time = 3600.0
        self.det_maxcount_time = 86400.0
        self.dwelltime = None
        self.comments = comments
        self.filename = filename
        self.auto_increment = auto_increment
        self.filetype = 'ASCII'
        self.scantype = 'linear'
        self.detmode  = 'scaler'
        self.scandb = scandb
        self.prescan_func = prescan_func
        self.verified = False
        self.abort = False
        self.pause = False
        self.inittime = 0 # time to initialize scan (pre_scan, move to start, begin i/o)
        self.looptime = 0 # time to run scan loop (even if aborted)
        self.exittime = 0 # time to complete scan (post_scan, return positioners, complete i/o)
        self.runtime  = 0 # inittime + looptime + exittime

        self.message_thread = None
        self.messenger = messenger or sys.stdout.write
        if filename is not None:
            self.datafile = self.open_output_file(filename=filename,
                                                  comments=comments)

        self.cpt = 0
        self.npts = 0
        self.complete = False
        self.debug = False
        self.message_points = 25
        self.extra_pvs = []
        self.positioners = []
        self.triggers = []
        self.counters = []
        self.detectors = []

        self.breakpoints = []
        self.at_break_methods = []
        self.pre_scan_methods = []
        self.post_scan_methods = []
        self.pos_actual  = []
        self.dtimer = debugtime()

    def set_info(self, attr, value):
        """set scan info to _scan variable"""
        if self.scandb is not None:
            self.scandb.set_info(attr, value)
            self.scandb.set_info('heartbeat', time.ctime())

    def enable_slewscan(self):
        print("Enabling Slew SCAN")
        if self.scantype in ('slew', 'qxafs'):
            conf = self.scandb.get_config(self.scantype)
            conf = self.slewscan_config = json.loads(conf.notes)
            self.xps = NewportXPS(conf['host'],
                                  username=conf['username'],
                                  password=conf['password'],
                                  group=conf['group'],
                                  outputs=conf['outputs'])


    def open_output_file(self, filename=None, comments=None):
        """opens the output file"""
        creator = ASCIIScanFile
        # if self.filetype == 'ASCII':
        #     creator = ASCIIScanFile
        if filename is not None:
            self.filename = filename
        if comments is not None:
            self.comments = comments

        return creator(name=self.filename,
                       auto_increment=self.auto_increment,
                       comments=self.comments, scan=self)

    def add_counter(self, counter, label=None):
        "add simple counter"
        if isinstance(counter, six.string_types):
            counter = Counter(counter, label)
        if counter not in self.counters:
            self.counters.append(counter)
        self.verified = False

    def add_trigger(self, trigger, label=None, value=1):
        "add simple detector trigger"
        if trigger is None:
            return
        if isinstance(trigger, six.string_types):
            trigger = Trigger(trigger, label=label, value=value)
        if trigger not in self.triggers:
            self.triggers.append(trigger)
        self.verified = False

    def add_extra_pvs(self, extra_pvs):
        """add extra pvs (tuple of (desc, pvname))"""
        if extra_pvs is None or len(extra_pvs) == 0:
            return
        for desc, pvname in extra_pvs:
            if isinstance(pvname, PV):
                pv = pvname
            else:
                pv = get_pv(pvname)

            if (desc, pv) not in self.extra_pvs:
                self.extra_pvs.append((desc, pv))

    def add_positioner(self, pos):
        """ add a Positioner """
        self.add_extra_pvs(pos.extra_pvs)
        self.at_break_methods.append(pos.at_break)
        self.post_scan_methods.append(pos.post_scan)
        self.pre_scan_methods.append(pos.pre_scan)

        if pos not in self.positioners:
            self.positioners.append(pos)
        self.verified = False

    def add_detector(self, det):
        """ add a Detector -- needs to be derived from Detector_Mixin"""
        if det.extra_pvs is None: # not fully connected!
            det.connect_counters()

        self.add_extra_pvs(det.extra_pvs)
        self.at_break_methods.append(det.at_break)
        self.post_scan_methods.append(det.post_scan)
        self.pre_scan_methods.append(det.pre_scan)
        self.add_trigger(det.trigger)
        for counter in det.counters:
            self.add_counter(counter)
        if det not in self.detectors:
            self.detectors.append(det)
        self.verified = False

    def set_dwelltime(self, dtime=None):
        """set scan dwelltime per point to constant value"""
        if dtime is not None:
            self.dwelltime = dtime
        for d in self.detectors:
            d.set_dwelltime(self.dwelltime)

    def at_break(self, breakpoint=0, clear=False):
        out = [m(breakpoint=breakpoint) for m in self.at_break_methods]
        if self.datafile is not None:
            self.datafile.write_data(breakpoint=breakpoint)
        return out

    def pre_scan(self, **kws):
        if self.debug: print('Stepscan PRE SCAN ')
        for (desc, pv) in self.extra_pvs:
            pv.connect()

        out = []
        for meth in self.pre_scan_methods:
            out.append( meth(scan=self))
            time.sleep(0.05)

        for det in self.detectors:
            for counter in det.counters:
                self.add_counter(counter)

        if callable(self.prescan_func):
            try:
                ret = self.prescan_func(scan=self)
            except:
                ret = None
            out.append(ret)
        return out

    def post_scan(self):
        if self.debug:
            print('Stepscan POST SCAN ')
        self.set_info('scan_progress', 'finishing')
        return [m() for m in self.post_scan_methods]

    def verify_scan(self):
        """ this does some simple checks of Scans, checking that
        the length of the positions array matches the length of the
        positioners array.

        For each Positioner, the max and min position is checked against
        the HLM and LLM field (if available)
        """
        npts = None
        for pos in self.positioners:
            if not pos.verify_array():
                self.set_error('Positioner {0} array out of bounds'.format(
                    pos.pv.pvname))
                return False
            if npts is None:
                npts = len(pos.array)
            if len(pos.array) != npts:
                self.set_error('Inconsistent positioner array length')
                return False
        return True


    def check_outputs(self, out, msg='unknown'):
        """ check outputs of a previous command
            Any True value indicates an error
        That is, return values must be None or evaluate to False
        to indicate success.
        """
        if not isinstance(out, (tuple, list)):
            out = [out]
        if any(out):
            raise Warning('error on output: %s' % msg)

    def read_extra_pvs(self):
        "read values for extra PVs"
        out = []
        for desc, pv in self.extra_pvs:
            out.append((desc, pv.pvname, pv.get(as_string=True)))
        return out

    def clear_data(self):
        """clear scan data"""
        for c in self.counters:
            c.clear()
        self.pos_actual = []

    def show_scan_progress(self, cpt, npts=0, **kws):
        time_left = (npts-cpt)* (self.pos_settle_time + self.det_settle_time)
        if self.dwelltime_varys:
            time_left += self.dwelltime[cpt:].sum()
        else:
            time_left += (npts-cpt)*self.dwelltime
        self.set_info('scan_time_estimate', time_left)
        time_est  = hms(time_left)
        if cpt < 4:
            self.set_info('filename', self.filename)
        msg = 'Point %i/%i,  time left: %s' % (cpt, npts, time_est)
        if cpt % self.message_points == 0:
            self.messenger("%s\n" % msg)
        self.set_info('scan_progress', msg)

    def set_all_scandata(self):
        self.publishing_scandata = True
        for c in self.counters:
            name = getattr(c, 'db_label', None)
            if name is None:
                name = c.label
            c.db_label = fix_varname(name)
            self.scandb.set_scandata(c.db_label, c.buff)
        self.publishing_scandata = False

    def publish_scandata(self, wait=False):
        "post scan data to db"
        if self.scandb is None:
            return
        if wait:
            self.set_all_scandata()
        else:
            self.publish_thread = Thread(target=self.set_all_scandata)
            self.publish_thread.start()

    def set_error(self, msg):
        """set scan error message"""
        if self.scandb is not None:
            self.set_info('last_error', msg)

    def set_scandata(self, attr, value):
        if self.scandb is not None:
            self.scandb.set_scandata(fix_varname(attr), value)

    def init_scandata(self):
        if self.scandb is None:
            return
        self.scandb.clear_scandata()
        self.scandb.commit()
        time.sleep(0.05)
        names = []
        npts = len(self.positioners[0].array)
        for p in self.positioners:
            try:
                units = p.pv.units
            except:
                units = 'unknown'

            name = fix_varname(p.label)
            if name in names:
                name += '_2'
            if name not in names:
                self.scandb.add_scandata(name, p.array.tolist(),
                                         pvname=p.pv.pvname,
                                         units=units, notes='positioner')
                names.append(name)
        for c in self.counters:
            try:
                units = c.pv.units
            except:
                units = 'counts'

            name = fix_varname(c.label)
            if name in names:
                name += '_2'
            if name not in names:
                self.scandb.add_scandata(name, [],
                                         pvname=c.pv.pvname,
                                         units=units, notes='counter')
                names.append(name)
        self.scandb.commit()

    def get_infobool(self, key):
        if self.scandb is not None:
            return self.scandb.get_info(key, as_bool=True)
        return False

    def look_for_interrupts(self):
        """set interrupt requests:

        abort / pause / resume

        if scandb is being used, these are looked up from database.
        otherwise local larch variables are used.
        """
        self.abort  = self.get_infobool('request_abort')
        self.pause  = self.get_infobool('request_pause')
        self.resume = self.get_infobool('request_resume')
        return self.abort

    def write(self, msg):
        self.messenger(msg)

    def clear_interrupts(self):
        """re-set interrupt requests:

        abort / pause / resume

        if scandb is being used, these are looked up from database.
        otherwise local larch variables are used.
        """
        self.abort = self.pause = self.resume = False
        self.set_info('request_abort', 0)
        self.set_info('request_pause', 0)
        self.set_info('request_resume', 0)

    def run(self, filename=None, comments=None, debug=False):
        """run scan"""
        print("Scan.RUN ", filename, self.scantype)
        runner = self.run_stepscan
        if self.scantype == 'slew':
            runner = self.run_slewscan
        runner(filename=filename, comments=comments, debug=debug)

    def prepare_stepscan(self):
        """prepare stepscan
        """
        self.pos_settle_time = max(MIN_POLL_TIME, self.pos_settle_time)
        self.det_settle_time = max(MIN_POLL_TIME, self.det_settle_time)

        if not self.verify_scan():
            self.write('Cannot execute scan: %s' % self._scangroup.error_message)
            self.set_info('scan_message', 'cannot execute scan')
            return

        self.clear_interrupts()
        self.dtimer.add('PRE: cleared interrupts')

        self.orig_positions = [p.current() for p in self.positioners]

        out = [p.move_to_start(wait=False) for p in self.positioners]
        self.check_outputs(out, msg='move to start')

        npts = len(self.positioners[0].array)
        self.message_points = min(100, max(10, 25*round(npts/250.0)))
        self.dwelltime_varys = False
        if self.dwelltime is not None:
            self.min_dwelltime = self.dwelltime
            self.max_dwelltime = self.dwelltime
            if isinstance(self.dwelltime, (list, tuple)):
                self.dwelltime = np.array(self.dwelltime)
            if isinstance(self.dwelltime, np.ndarray):
                self.min_dwelltime = min(self.dwelltime)
                self.max_dwelltime = max(self.dwelltime)
                self.dwelltime_varys = True

        time_est = npts*(self.pos_settle_time + self.det_settle_time)
        if self.dwelltime_varys:
            time_est += self.dwelltime.sum()
            for d in self.detectors:
                d.set_dwelltime(self.dwelltime[0])
        else:
            time_est += npts*self.dwelltime
            for d in self.detectors:
                d.set_dwelltime(self.dwelltime)

        if self.scandb is not None:
            self.set_info('scan_progress', 'preparing scan')

        out = self.pre_scan()
        self.check_outputs(out, msg='pre scan')

        self.datafile = self.open_output_file(filename=self.filename,
                                              comments=self.comments)

        self.datafile.write_data(breakpoint=0)
        self.filename =  self.datafile.filename
        self.set_info('filename', self.filename)

        self.clear_data()
        if self.scandb is not None:
            self.init_scandata()
            self.set_info('request_abort', 0)
            self.set_info('scan_time_estimate', time_est)
            self.set_info('scan_total_points', npts)

        self.dtimer.add('PRE: wrote data 0')
        self.set_info('scan_progress', 'starting scan')

        self.message_thread = None
        if callable(self.messenger):
            self.message_thread = ScanMessenger(func=self.show_scan_progress,
                                                scan = self, npts=npts, cpt=0)
            self.message_thread.start()
        self.cpt = 0
        self.npts = npts

        out = [p.move_to_start(wait=True) for p in self.positioners]
        self.check_outputs(out, msg='move to start, wait=True')
        [p.current() for p in self.positioners]
        [d.pv.get() for d in self.counters]
        self.dtimer.add('PRE: start scan')

    def run_stepscan(self, filename=None, comments=None, debug=False):
        """ run a stepscan:
           Verify, Save original positions,
           Setup output files and messenger thread,
           run pre_scan methods
           Loop over points
           run post_scan methods
        """
        if filename is not None:
            self.filename  = filename
        if comments is not None:
            self.comments = comments

        self.complete = False
        self.dtimer = debugtime(verbose=debug)
        self.publishing_scandata = False
        self.publish_thread = None

        ts_start = time.time()
        self.prepare_stepscan()
        ts_init = time.time()
        self.inittime = ts_init - ts_start

        i = -1
        while not self.abort:
            i += 1
            if i >= self.npts:
                break
            try:
                point_ok = True
                self.cpt = i+1
                self.look_for_interrupts()
                self.dtimer.add('Pt %i : looked for interrupts' % i)
                while self.pause:
                    time.sleep(0.25)
                    if self.look_for_interrupts():
                        break
                # set dwelltime
                if self.dwelltime_varys:
                    for d in self.detectors:
                        d.set_dwelltime(self.dwelltime[i])
                # move to next position
                [p.move_to_pos(i) for p in self.positioners]
                self.dtimer.add('Pt %i : move_to_pos (%i)' % (i, len(self.positioners)))
                # publish scan data
                if i > 1 and not self.publishing_scandata:
                    self.publish_scandata()

                # move positioners
                t0 = time.time()
                while (not all([p.done for p in self.positioners]) and
                       time.time() - t0 < self.pos_maxmove_time):
                    if self.look_for_interrupts():
                        break
                    poll(MIN_POLL_TIME, 0.25)
                self.dtimer.add('Pt %i : pos done' % i)
                poll(self.pos_settle_time, 0.25)
                self.dtimer.add('Pt %i : pos settled' % i)

                # trigger detectors
                [trig.start() for trig in self.triggers]
                self.dtimer.add('Pt %i : triggers fired, (%d)' % (i, len(self.triggers)))

                # wait for detectors
                t0 = time.time()
                time.sleep(max(0.05, self.min_dwelltime/2.0))
                while not all([trig.done for trig in self.triggers]):
                    if (time.time() - t0) > 5.0*(1 + 2*self.max_dwelltime):
                        break
                    poll(MIN_POLL_TIME, 0.5)
                self.dtimer.add('Pt %i : triggers done' % i)
                if self.look_for_interrupts():
                    break
                point_ok = (all([trig.done for trig in self.triggers]) and
                            time.time()-t0 > (0.75*self.min_dwelltime))
                if not point_ok:
                    point_ok = True
                    time.sleep(0.25)
                    poll(0.1, 2.0)
                    for trig in self.triggers:
                        poll(10*MIN_POLL_TIME, 1.0)
                        point_ok = point_ok and (trig.runtime > (0.8*self.min_dwelltime))
                        if not point_ok:
                            print('Trigger problem?:', trig, trig.runtime, self.min_dwelltime)
                            trig.abort()

                # read counters and actual positions
                poll(self.det_settle_time, 0.1)
                self.dtimer.add('Pt %i : det settled done.' % i)
                [c.read() for c in self.counters]
                self.dtimer.add('Pt %i : read counters' % i)

                self.pos_actual.append([p.current() for p in self.positioners])
                if self.message_thread is not None:
                    self.message_thread.cpt = self.cpt
                self.dtimer.add('Pt %i : sent message' % i)

                # if this is a breakpoint, execute those functions
                if i in self.breakpoints:
                    self.at_break(breakpoint=i, clear=True)
                self.dtimer.add('Pt %i: done.' % i)
                self.look_for_interrupts()

            except KeyboardInterrupt:
                self.set_info('request_abort', 1)
                self.abort = True
            if not point_ok:
                self.write('point messed up.  Will try again\n')
                time.sleep(0.25)
                for trig in self.triggers:
                    trig.abort()
                for det in self.detectors:
                    det.pre_scan(scan=self)
                i -= 1
            if self.publish_thread is not None:
                self.publish_thread.join()
            self.dtimer.add('Pt %i: completely done.' % i)

        # scan complete
        # return to original positions, write data
        self.dtimer.add('Post scan start')
        self.publish_scandata(wait=True)
        ts_loop = time.time()
        self.looptime = ts_loop - ts_init

        for val, pos in zip(self.orig_positions, self.positioners):
            pos.move_to(val, wait=False)
        self.dtimer.add('Post: return move issued')
        self.datafile.write_data(breakpoint=-1, close_file=True, clear=False)
        self.dtimer.add('Post: file written')
        if self.look_for_interrupts():
            self.write("scan aborted at point %i of %i." % (self.cpt, self.npts))
            raise ScanDBAbort("scan aborted")

        # run post_scan methods
        out = self.post_scan()
        self.check_outputs(out, msg='post scan')
        self.dtimer.add('Post: post_scan done')
        self.complete = True

        # end messenger thread
        if self.message_thread is not None:
            self.message_thread.cpt = None
            self.message_thread.join()

        self.set_info('scan_progress',
                      'scan complete. Wrote %s' % self.datafile.filename)
        ts_exit = time.time()
        self.exittime = ts_exit - ts_loop
        self.runtime  = ts_exit - ts_start
        self.dtimer.add('Post: fully done')

        if debug:
            self.dtimer.show()
        return self.datafile.filename

    def prepare_slewscan(self):
        """prepare slew scan"""

        currscan = 'CurrentScan.ini'
        server  = self.scandb.get_info('server_fileroot')
        workdir = self.scandb.get_info('user_folder')
        basedir = os.path.join(server, workdir, 'Maps')
        if not os.path.exists(basedir):
            os.mkdir(basedir)
        sname = os.path.join(server, workdir, 'Maps', currscan)
        oname = os.path.join(server, workdir, 'Maps', 'PreviousScan.ini')
        if os.path.exists(sname):
            shutil.copy(sname, oname)
        txt = ['# FastMap configuration file (saved: %s)'%(time.ctime()),
               '#-------------------------#',  '[scan]',
               'filename = %s' % self.filename,
               'comments = %s' % self.comments]

        dim  = 1
        if self.outer is not None:
            dim = 2
        l_, pvs, start, stop, npts = self.inner
        pospv = pvs[0]
        if pospv.endswith('.VAL'):
            pospv = pospv[:-4]
        step = abs(start-stop)/(npts-1)
        dtime = self.dwelltime*(npts-1)
        txt.append('dimension = %i' % dim)
        txt.append('pos1 = %s'     % pospv)
        txt.append('start1 = %.4f' % start)
        txt.append('stop1 = %.4f'  % stop)
        txt.append('step1 = %.4f'  % step)
        txt.append('time1 = %.4f'  % dtime)

        axis = None
        for ax, pvname in self.slewscan_config['motors'].items():
            if pvname == pospv:
                axis = ax

        if axis is None:
            raise ValueError("Could not find XPS Axis for %s" % pospv)

        self.xps.define_line_trajectories(axis,
                                          start=start, stop=stop,
                                          step=step, scantime=dtime)

        if dim == 2:
            l_, pvs, start, stop, npts = self.outer
            pospv = pvs[0]
            if pospv.endswith('.VAL'):
                pospv = pospv[:-4]
            step = abs(start-stop)/(npts-1)
            txt.append('pos2 = %s'   % pospv)
            txt.append('start2 = %.4f' % start)
            txt.append('stop2 = %.4f' % stop)
            txt.append('step2 = %.4f' % step)

        txt.append('#------------------#')
        txt.append('[xrd_ad]')
        xrd_det = None
        for det in self.detectors:
            if isinstance(det, AreaDetector):
                xrd_det = det

        if xrd_det is None:
            txt.append('use = False')
        else:
            txt.append('use = True')
            txt.append('type = PEDET1')
            txt.append('prefix = %s' % det.prefix)
            txt.append('fileplugin = netCDF1:')

        f = open(sname, 'w')
        f.write('\n'.join(txt))
        f.close()
        print("Wrote Simple Scan Config: ", sname)
        return sname

    def run_slewscan(self, filename='map.001', comments=None):
        """
        run a slew scan
        """
        self.prepare_slewscan()
        self.xps.arm_trajectory('backward')
        for det in self.detectors:
            det.Arm(mode=self.detmode)

        print("Ready for SLEWSCAN !! ")

        self.clear_interrupts()
        self.set_info('scan_progress', 'starting')
        return

        # watch scan
        # first, wait for scan to start (status == 2)
        collecting = False
        t0 = time.time()
        while not collecting and time.time()-t0 < 120:

            collecting = (2 == caget('%sstatus' % mapper))
            time.sleep(0.25)
            if self.look_for_interrupts():
                break
        if self.abort:
            caput("%sAbort" % mapper, 1)

        nrow = 0
        t0 = time.time()
        maxrow = caget('%smaxrow' % mapper)
        info = caget("%sinfo" % mapper, as_string=True)
        self.set_info('scan_progress', info)
        #  wait for scan to get past row 1
        while nrow < 1 and time.time()-t0 < 120:
            nrow = caget('%snrow' % mapper)
            time.sleep(0.25)
            if self.look_for_interrupts():
                break
        if self.abort:
            caput("%sAbort" % mapper, 1)

        maxrow  = caget("%smaxrow" % mapper)
        time.sleep(1.0)
        fname  = caget("%sfilename" % mapper, as_string=True)
        self.set_info('filename', fname)

        # wait for map to finish:
        # must see "status=Idle" for 10 consequetive seconds
        collecting_map = True
        nrowx, nrow = 0, 0
        t0 = time.time()
        while collecting_map:
            time.sleep(0.25)
            status_val = caget("%sstatus" % mapper)
            status_str = caget("%sstatus" % mapper, as_string=True)
            nrow       = caget("%snrow" % mapper)
            self.set_info('scan_status', status_str)
            time.sleep(0.25)
            if self.look_for_interrupts():
                break
            if nrowx != nrow:
                info = caget("%sinfo" % mapper, as_string=True)
                self.set_info('scan_progress', info)
                nrowx = nrow
            if status_val == 0:
                collecting_map = ((time.time() - t0) < 10.0)
            else:
                t0 = time.time()

        # if aborted from ScanDB / ScanGUI wait for status
        # to go to 0 (or 5 minutes)
        self.look_for_interrupts()
        if self.abort:
            caput('%sAbort' % mapper, 1)
            time.sleep(0.5)
            t0 = time.time()
            status_val = caget('%sstatus' % mapper)
            while status_val != 0 and (time.time()-t0 < 10.0):
                time.sleep(0.25)
                status_val = caget('%sstatus' % mapper)

        status_strg = caget('%sstatus' % mapper, as_string=True)
        self.set_info('scan_status', status_str)
        if self.abort:
            raise ScanDBAbort("slewscan aborted")
        return