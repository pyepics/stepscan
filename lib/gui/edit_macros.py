import os
import sys
import time
import logging
from sqlalchemy.sql import func as sqlfunc
from sqlalchemy import text
from datetime import datetime, timedelta
import wx
import wx.lib.agw.flatnotebook as flat_nb
import wx.lib.scrolledpanel as scrolled
from wx.lib.editor import Editor
import wx.dataview as dv

DVSTYLE = dv.DV_VERT_RULES|dv.DV_ROW_LINES|dv.DV_MULTIPLE

from collections import OrderedDict
import epics
from .gui_utils import (GUIColors, set_font_with_children, YesNo,
                        add_menu, add_button, add_choice, pack, SimpleText,
                        FileOpen, FileSave, popup, FloatCtrl,
                        FRAMESTYLE, Font)

from .common_commands  import CommonCommandsFrame, CommonCommandsAdminFrame
from .edit_sequences   import ScanSequenceFrame
from ..scandb import InstrumentDB

import larch
from larch.wxlib.readlinetextctrl import ReadlineTextCtrl
LEFT = wx.ALIGN_LEFT|wx.ALIGN_CENTER_VERTICAL|wx.ALL
CEN  = wx.ALIGN_CENTER|wx.ALIGN_CENTER_VERTICAL|wx.ALL

ALL_EXP  = wx.ALL|wx.EXPAND
LEFT_CEN = wx.ALIGN_LEFT|wx.ALIGN_CENTER_VERTICAL
FNB_STYLE = flat_nb.FNB_NO_X_BUTTON|flat_nb.FNB_SMART_TABS|flat_nb.FNB_NO_NAV_BUTTONS|flat_nb.FNB_NODRAG

AUTOSAVE_FILE = 'macros_autosave.lar'
MACRO_HISTORY = 'scan_macro_history.lar'
LONG_AGO = datetime.now()-timedelta(2000)
COLOR_MSG  = '#0099BB'
COLOR_OK   = '#0000BB'
COLOR_WARN = '#BB9900'
COLOR_ERR  = '#BB0000'

def cmp(a, b):
    return (a>b)-(b<a)

class ScanDBMessageQueue(object):
    """ScanDB Messages"""
    def __init__(self, scandb):
        self.scandb = scandb
        self.cls, self.tab = scandb.get_table('messages')
        # get last ID
        out = scandb.query(sqlfunc.max(self.cls.id)).one()
        self.last_id = out[0]

    def get_new_messages(self):
        try:
            q = self.tab.select(whereclause=text("id>'%i'" % self.last_id))
        except TypeError:
            return [None]
        out = q.order_by(self.cls.id).execute().fetchall()
        if len(out) > 0:
            self.last_id = out[-1].id
        return out

def get_positionlist(scandb, instrument='SampleStage'):
    """get list of positions for and instrument"""
    return InstrumentDB(scandb).get_positionlist(instrument)

class PositionCommandModel(dv.DataViewIndexListModel):
    def __init__(self, scandb):
        dv.DataViewIndexListModel.__init__(self, 0)
        self.scandb = scandb
        self.data = []
        self.posvals = {}
        self.read_data()

    def read_data(self):
        self.data = []
        for pos in get_positionlist(self.scandb):
            use, nscan = True, '1'
            if pos in self.posvals:
                use, nsscan = self.posvals[pos]
            self.data.append([pos, use, nscan])
            self.posvals[pos] = [use, nscan]
        self.data.reverse()
        self.Reset(len(self.data))

    def GetColumnType(self, col):
        if col == 1:
            return "bool"
        return "string"

    def GetValueByRow(self, row, col):
        return self.data[row][col]

    def SetValueByRow(self, value, row, col):
        self.data[row][col] = value
        return True

    def GetColumnCount(self):
        return len(self.data[0])

    def GetCount(self):
        return len(self.data)

    def Compare(self, item1, item2, col, ascending):
        """help for sorting data"""
        if not ascending: # swap sort order?
            item2, item1 = item1, item2
        row1 = self.GetRow(item1)
        row2 = self.GetRow(item2)
        if col == 1:
            return cmp(int(self.data[row1][col]), int(self.data[row2][col]))
        else:
            return cmp(self.data[row1][col], self.data[row2][col])

    def DeleteRows(self, rows):
        rows = list(rows)
        rows.sort(reverse=True)
        for row in rows:
            del self.data[row]
            self.RowDeleted(row)

    def AddRow(self, value):
        self.data.append(value)
        self.RowAppended()

class PositionCommandFrame(wx.Frame) :
    """Edit/Manage/Run/View Sequences"""
    def __init__(self, parent, scandb, pos=(-1, -1), size=(700, 550), _larch=None):
        self.parent = parent
        self.scandb = scandb
        self.last_refresh = time.monotonic() - 100.0
        self.Font10=wx.Font(10, wx.SWISS, wx.NORMAL, wx.BOLD, 0, "")
        titlefont = wx.Font(12, wx.SWISS, wx.NORMAL, wx.BOLD, 0, "")

        wx.Frame.__init__(self, None, -1,
                          title="Data Collection Commands at Saved Positions",
                          style=FRAMESTYLE, size=size)

        self.SetFont(self.Font10)
        panel = scrolled.ScrolledPanel(self, size=(700, 500))
        self.colors = GUIColors()
        panel.SetBackgroundColour(self.colors.bg)

        self.dvc = dv.DataViewCtrl(panel, style=DVSTYLE)
        self.dvc.SetMinSize((700, 450))

        self.model = PositionCommandModel(self.scandb)
        self.dvc.AssociateModel(self.model)

        self.datatype = add_choice(panel, ['Scan', 'XRD'], size=(125, -1),
                                   action=self.onDataType)
        self.datatype.SetSelection(0)

        self.scantype = add_choice(panel,  ('Maps', 'XAFS', 'Linear'),
                                   size=(125, -1),  action = self.onScanType)
        self.scantype.SetSelection(1)

        self.scanname = add_choice(panel,  [], size=(250, -1))
        self.xrdtime = FloatCtrl(panel, value=10, minval=0, maxval=50000, precision=1)
        self.xrdtime.Disable()

        sizer = wx.GridBagSizer(2, 2)

        irow = 0
        sizer.Add(add_button(panel, label='Select All', size=(125, -1),
                             action=self.onSelAll),
                  (irow, 0), (1, 1), LEFT_CEN, 2)
        sizer.Add(add_button(panel, label='Select None', size=(125, -1),
                             action=self.onSelNone),
                  (irow, 1), (1, 1), LEFT_CEN, 2)
        sizer.Add(add_button(panel, label='Add Commands', size=(150, -1),
                             action=self.onInsert),
                  (irow, 2), (1, 2), LEFT_CEN, 2)

        irow += 1
        sizer.Add(SimpleText(panel, 'Command Type:'), (irow, 0), (1, 1), LEFT_CEN, 2)
        sizer.Add(self.datatype,                      (irow, 1), (1, 1), LEFT_CEN, 2)
        sizer.Add(SimpleText(panel, 'XRD Time (sec):'), (irow, 2), (1, 1), LEFT_CEN, 2)
        sizer.Add(self.xrdtime,                       (irow, 3), (1, 1), LEFT_CEN, 2)

        irow += 1
        sizer.Add(SimpleText(panel, 'Scan Type:'),    (irow, 0), (1, 1), LEFT_CEN, 2)
        sizer.Add(self.scantype,                      (irow, 1), (1, 1), LEFT_CEN, 2)
        sizer.Add(SimpleText(panel, 'Scan Name:'),    (irow, 2), (1, 1), LEFT_CEN, 2)
        sizer.Add(self.scanname,                      (irow, 3), (1, 1), LEFT_CEN, 2)


        for icol, dat in enumerate((('Position Name',  400, 'text'),
                                    ('Include',        100, 'bool'),
                                    ('# Scans',        100, 'text'))):
            label, width, dtype = dat
            method = self.dvc.AppendTextColumn
            mode = dv.DATAVIEW_CELL_EDITABLE
            if dtype == 'bool':
                method = self.dvc.AppendToggleColumn
                mode = dv.DATAVIEW_CELL_ACTIVATABLE
            kws = {}
            if icol > 0:
                kws['mode'] = mode
            method(label, icol, width=width, **kws)
            c = self.dvc.Columns[icol]
            c.Alignment = wx.ALIGN_LEFT
            c.Sortable = False

        irow += 1
        sizer.Add(self.dvc, (irow, 0), (2, 5), LEFT_CEN, 2)
        pack(panel, sizer)

        panel.SetupScrolling()

        mainsizer = wx.BoxSizer(wx.VERTICAL)
        mainsizer.Add(panel, 1, wx.GROW|wx.ALL, 1)
        pack(self, mainsizer)
        self.dvc.EnsureVisible(self.model.GetItem(0))

        self.Bind(wx.EVT_CLOSE, self.onClose)
        self.timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self.onTimer, self.timer)
        self.timer.Start(5000)
        wx.CallAfter(self.onScanType)
        self.Show()
        self.Raise()

    def onDataType(self, event=None):
        name = self.datatype.GetStringSelection().lower()
        self.xrdtime.Enable(name=='xrd')
        self.scantype.Enable(name=='scan')
        self.scanname.Enable(name=='scan')

    def onScanType(self, event=None):
        sname = self.scantype.GetStringSelection().lower()
        scantype = 'linear'
        if 'xafs' in sname:
            scantype = 'xafs'
        elif 'map' in sname or 'slew' in sname:
            scantype = 'slew'
        cls, table = self.scandb.get_table('scandefs')
        q = table.select().where(table.c.type.ilike("%%%s%%" % scantype)).order_by('last_used_time')
        scannames = []
        for s in q.execute().fetchall():
            if not (s.name.startswith('__') and s.name.endswith('__')):
                scannames.append(s.name)
        scannames.reverse()
        self.scanname.Set(scannames)
        self.scanname.SetSelection(0)

    def onInsert(self, event=None):
        datatype = self.datatype.GetStringSelection()
        buff = ["# commands added at positions"]
        if datatype == 'xrd':
            xrdtime =  self.xrdtime.GetValue()
            command = "xrd_at(%s, t=%.1f)"
            for posname, use, nscans in self.model.data:
                if use:
                    buff.append(command % (repr(posname), xrdtime))
        else:
            scanname = self.scanname.GetStringSelection()
            command = "pos_scan(%s, %s, number=%s)"
            for posname, use, nscans in self.model.data:
                if use:
                    buff.append(command % (repr(posname), repr(scanname), nscans))
        buff.append("")
        try:
            self.parent.subframes['macro'].editor.AppendText("\n".join(buff))
        except:
            print("No editor?")

    def onSelAll(self, event=None):
        for p in self.model.posvals.values():
            p[0] = True
        self.update()

    def onSelNone(self, event=None):
        for p in self.model.posvals.values():
            p[0] = False
        self.update()

    def onClose(self, event=None):
        self.timer.Stop()
        time.sleep(1.0)
        self.Destroy()

    def onTimer(self, event=None, **kws):
        now = time.monotonic()
        poslist = get_positionlist(self.scandb)
        if len(self.model.data) != len(poslist):
            self.update()

    def update(self):
        self.model.read_data()
        self.Refresh()
        self.dvc.EnsureVisible(self.model.GetItem(0))



class MacroFrame(wx.Frame) :
    """Edit/Manage Macros (Larch Code)"""
    output_colors = {'error_message': COLOR_ERR,
                     'scan_message':COLOR_OK}
    output_fields = ('error_message', 'scan_message')

    info_mapping = {'FileName': 'filename',
                    'Command': 'current_command',
                    'Status': 'scan_status',
                    'Progress': 'scan_progress',
                    'Time': 'heartbeat'}

    def __init__(self, parent, scandb=None, _larch=None,
                 pos=(-1, -1), size=(850, 600)):

        self.parent = parent
        self.scandb = parent.scandb if scandb is None else scandb
        self.subframes = {}
        self.winfo = OrderedDict()
        self.output_stats = {}
        self.last_heartbeat = LONG_AGO
        self.last_start_request = 0
        for key in self.output_fields:
            self.output_stats[key] = LONG_AGO

        wx.Frame.__init__(self, None, -1,  title='Epics Scanning: Macro',
                          style=FRAMESTYLE, size=size)

        self.SetFont(Font(10))
        sizer = wx.BoxSizer(wx.VERTICAL)

        self.createMenus()
        self.db_messages = ScanDBMessageQueue(self.scandb)
        self.colors = GUIColors()
        self.SetBackgroundColour(self.colors.bg)

        self.editor = wx.TextCtrl(self, -1, size=(600, 275),
                                  style=wx.TE_MULTILINE|wx.TE_RICH2)
        self.editor.SetBackgroundColour('#FFFFFF')

        text = """## Edit Macro text here\n#\n"""
        self.editor.SetValue(text)
        self.editor.SetInsertionPoint(len(text)-2)
        self.ReadMacroFile(AUTOSAVE_FILE)

        sfont = wx.Font(11,  wx.SWISS, wx.NORMAL, wx.BOLD, False)
        self.output = wx.TextCtrl(self, -1,  '## Output Buffer\n', size=(600, 275),
                                  style=wx.TE_MULTILINE|wx.TE_RICH|wx.TE_READONLY)
        self.output.CanCopy()
        self.output.SetInsertionPointEnd()
        self.output.SetDefaultStyle(wx.TextAttr('black', 'white', sfont))

        sizer.Add(self.make_info(),    0, LEFT|wx.GROW|wx.ALL, 3)
        sizer.Add(self.make_buttons(), 0, LEFT|wx.GROW|wx.ALL, 3)
        sizer.Add(self.editor, 1, CEN|wx.GROW|wx.ALL, 3)
        sizer.Add(self.output, 1, CEN|wx.GROW|wx.ALL, 3)


        sizer.Add(self.InputPanel(),  0, border=2,
                  flag=wx.ALIGN_CENTER_VERTICAL|wx.ALL|wx.EXPAND)


        self._stimer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self.update_info, self._stimer)
        self._stimer.Start(500)

        self.Bind(wx.EVT_CLOSE, self.onClose)
        self.SetMinSize((600, 520))
        pack(self, sizer)
        self.Show()
        self.Raise()

    def reload_macros(self):
        self.scandb.add_command('load_macros()')

    def update_info(self, evt=None):
        paused = self.scandb.get_info('request_pause', as_bool=True)

        for key, attr in self.info_mapping.items():
            val = str(self.scandb.get_info(attr, '--'))
            if key in self.winfo:
                self.winfo[key].SetLabel(val)

        # move_to_macro_editor = """
        for msg in self.db_messages.get_new_messages():
            if msg is not None:
                self.writeOutput(msg.text, color=COLOR_MSG, with_nl=False)

        for key in self.output_fields:
            row = self.scandb.get_info(key, full_row=True)
            mtime = self.output_stats.get(key, LONG_AGO)
            if row.modify_time > mtime:
                self.output_stats[key] = row.modify_time
                if len(row.value) > 0:
                    self.writeOutput(row.value,
                                     color=self.output_colors.get(key, None))

        row = self.scandb.get_info('heartbeat', full_row=True)
        if row.modify_time > self.last_heartbeat:
            self.last_heartbeat = row.modify_time
        col = COLOR_OK
        if self.last_heartbeat < datetime.now()-timedelta(seconds=15):
            col = COLOR_WARN
        if self.last_heartbeat < datetime.now()-timedelta(seconds=120):
            col = COLOR_ERR
        self.winfo['Time'].SetForegroundColour(col)

    def make_info(self):
        panel = wx.Panel(self)
        sizer = wx.GridBagSizer(2, 2)

        self.winfo = OrderedDict()
        opts1 = {'label':' '*250, 'colour': COLOR_OK, 'size': (600, -1), 'style': LEFT}
        opts2 = {'label':' '*50, 'colour': COLOR_OK, 'size': (200, -1), 'style': LEFT}
        self.winfo['FileName'] = SimpleText(panel, **opts1)
        self.winfo['Command']  = SimpleText(panel, **opts1)
        self.winfo['Progress'] = SimpleText(panel, **opts1)
        self.winfo['Status']   = SimpleText(panel, **opts2)
        self.winfo['Time']     = SimpleText(panel, **opts2)

        irow, icol = 0, 0
        for attr in ('Status', 'Time'):
            lab  = SimpleText(panel, "%s:" % attr, size=(100, -1), style=LEFT)
            sizer.Add(lab,               (irow, icol),   (1, 1), LEFT, 1)
            sizer.Add(self.winfo[attr],  (irow, icol+1), (1, 1), LEFT, 1)
            icol +=2

        irow += 1
        icol = 0
        for attr in ('Command', 'FileName', 'Progress'):
            lab  = SimpleText(panel, "%s:" % attr, size=(100, -1), style=LEFT)
            sizer.Add(lab,               (irow, 0), (1, 1), LEFT, 1)
            sizer.Add(self.winfo[attr],  (irow, 1), (1, 3), LEFT, 1)
            irow += 1

        pack(panel, sizer)
        return panel

    def make_buttons(self):
        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.HORIZONTAL)
        self.start_btn  = add_button(panel, label='Submit',  action=self.onStart)
        self.pause_btn  = add_button(panel, label='Pause',  action=self.onPause)
        self.resume_btn = add_button(panel, label='Resume',  action=self.onResume)
        self.cancel_btn = add_button(panel, label='Cancel All', action=self.onCancelAll)
        # self.abort_btn  = add_button(panel, label='Abort Command',  action=self.onAbort)
        # self.restart_btn = add_button(panel, label='Restart Server',
        # action=self.onRestartServer)

        sizer.Add(self.start_btn,  0, wx.ALIGN_LEFT, 2)
        sizer.Add(self.pause_btn,  0, wx.ALIGN_LEFT, 2)
        sizer.Add(self.resume_btn, 0, wx.ALIGN_LEFT, 2)
        sizer.Add(self.cancel_btn, 0, wx.ALIGN_LEFT, 2)
        # sizer.Add(self.abort_btn,  0, wx.ALIGN_LEFT, 2)
        # sizer.Add(self.restart_btn, 0, wx.ALIGN_LEFT, 2)
        pack(panel, sizer)
        return panel

    def createMenus(self):
        self.menubar = wx.MenuBar()
        # file
        fmenu = wx.Menu()
        add_menu(self, fmenu, "Read Macro\tCtrl+R",
                 "Read Macro", self.onReadMacro)

        add_menu(self, fmenu, "Save Macro\tCtrl+S",
                 "Save Macro", self.onSaveMacro)

        fmenu.AppendSeparator()
        add_menu(self, fmenu, "Quit\tCtrl+Q",
                 "Quit Macro", self.onClose)

        # commands
        pmenu = wx.Menu()
        add_menu(self, pmenu, "Show Command Sequence",  "Show Queue of Commands",
                 self.onEditSequence)
        add_menu(self, pmenu, "Add Common Commands",
                 "Common Commands", self.onCommonCommands)
        add_menu(self, pmenu, "Scan at Selected Positions",
                 "Position Scans", self.onBuildPosScan)
        pmenu.AppendSeparator()
        add_menu(self, pmenu, "Admin Common Commands",
                 "Admin Common Commands", self.onCommonCommandsAdmin)

        smenu = wx.Menu()

        self.menubar.Append(fmenu, "&File")
        self.menubar.Append(pmenu, "Commands and Sequence")
        self.SetMenuBar(self.menubar)

    def InputPanel(self):
        panel = wx.Panel(self, -1)
        self.prompt = wx.StaticText(panel, -1, ' >>>', size = (30,-1),
                                    style=wx.ALIGN_CENTER|wx.ALIGN_RIGHT)
        self.histfile = os.path.join(larch.site_config.usr_larchdir, MACRO_HISTORY)
        self.input = ReadlineTextCtrl(panel, -1,  '', size=(525,-1),
                                      historyfile=self.histfile,
                                      style=wx.ALIGN_LEFT|wx.TE_PROCESS_ENTER)

        self.input.Bind(wx.EVT_TEXT_ENTER, self.onText)
        sizer = wx.BoxSizer(wx.HORIZONTAL)

        sizer.Add(self.prompt,  0, wx.BOTTOM|wx.CENTER)
        sizer.Add(self.input,   1, wx.ALIGN_LEFT|wx.ALIGN_CENTER|wx.EXPAND)
        panel.SetSizer(sizer)
        sizer.Fit(panel)
        return panel

    def onText(self, event=None):
        text = event.GetString().strip()
        if len(text) < 1:
            return
        self.input.Clear()
        self.input.AddToHistory(text)
        out = self.scandb.add_command(text)
        self.scandb.commit()
        time.sleep(0.01)
        self.writeOutput(text)

    def writeOutput(self, text, color=None, with_nl=True):
        pos0 = self.output.GetLastPosition()
        if with_nl and not text.endswith('\n'):
            text = '%s\n' % text
        self.output.WriteText(text)
        if color is not None:
            style = self.output.GetDefaultStyle()
            bgcol = style.GetBackgroundColour()
            sfont = style.GetFont()
            pos1  = self.output.GetLastPosition()
            self.output.SetStyle(pos0, pos1, wx.TextAttr(color, bgcol, sfont))
        self.output.SetInsertionPoint(self.output.GetLastPosition())
        self.output.Refresh()

    def onBuildPosScan(self, event=None):
        # self.parent.show_subframe('buildposmacro', PosScanMacroBuilder)
        self.parent.show_subframe('buildposmacro', PositionCommandFrame)

    def onBuildPosXRD(self, event=None):
        self.parent.show_subframe('buildxrdsmacro', PosXRDMacroBuilder)

    def onCommonCommands(self, evt=None):
        self.parent.show_subframe('commands', CommonCommandsFrame)

    def onCommonCommandsAdmin(self, evt=None):
        self.parent.show_subframe('commands_admin', CommonCommandsAdminFrame)

    def onEditSequence(self, evt=None):
        self.parent.show_subframe('sequence', ScanSequenceFrame)

    def onReadMacro(self, event=None):
        wcard = 'Scan files (*.lar)|*.lar|All files (*.*)|*.*'
        fname = FileOpen(self, "Read Macro from File",
                         default_file='macro.lar',
                         wildcard=wcard)
        if fname is not None:
            self.ReadMacroFile(fname)

    def ReadMacroFile(self, fname):
        if os.path.exists(fname):
            try:
                text = open(fname, 'r').read()
            except:
                logging.exception('could not read MacroFile %s' % fname)
            finally:
                self.editor.SetValue(text)
                self.editor.SetInsertionPoint(len(text)-2)

    def onSaveMacro(self, event=None):
        wcard = 'Scan files (*.lar)|*.lar|All files (*.*)|*.*'
        fname = FileSave(self, 'Save Macro to File',
                         default_file='macro.lar', wildcard=wcard)
        fname = os.path.join(os.getcwd(), fname)
        if fname is not None:
            if os.path.exists(fname):
                ret = popup(self, "Overwrite Macro File '%s'?" % fname,
                            "Really Overwrite Macro File?",
                            style=wx.YES_NO|wx.NO_DEFAULT|wx.ICON_QUESTION)
                if ret != wx.ID_YES:
                    return
            self.SaveMacroFile(fname)

    def SaveMacroFile(self, fname):
        try:
            fh = open(fname, 'w')
            fh.write('%s\n' % self.editor.GetValue())
            fh.close()
        except:
            print('could not save MacroFile %s' % fname)

    def onStart(self, event=None):
        now = time.time()
        if (now - self.last_start_request) < 5.0:
            print( "double clicked start?")
            return
        self.last_start_request = now
        # self.start_btn.Disable()
        lines = self.editor.GetValue().split('\n')
        self.scandb.set_info('request_pause',  1)

        for lin in lines:
            if '#' in lin:
                icom = lin.index('#')
                lin = lin[:icom]
            lin = lin.strip()
            if len(lin) > 0:
                self.scandb.add_command(lin)
        self.scandb.commit()
        self.scandb.set_info('request_abort',  0)
        self.scandb.set_info('request_pause',  0)

    def onPause(self, event=None):
        self.scandb.set_info('request_pause', 1)
        self.scandb.commit()
        self.pause_btn.Disable()
        # self.start_btn.Disable()
        self.resume_btn.SetBackgroundColour("#D1D122")

    def onResume(self, event=None):
        self.scandb.set_info('request_pause', 0)
        self.scandb.commit()
        self.pause_btn.Enable()
        # self.start_btn.Enable()
        fg = self.pause_btn.GetBackgroundColour()
        self.resume_btn.SetBackgroundColour(fg)

    def onAbort(self, event=None):
        self.scandb.set_info('request_abort', 1)
        self.scandb.commit()
        time.sleep(1.0)

    def onCancelAll(self, event=None):
        self.onPause()
        self.scandb.set_info('request_abort', 1)
        self.scandb.cancel_remaining_commands()
        time.sleep(1.0)
        self.onResume()

    def onRestartServer(self, event=None):
        self.onPause()
        self.scandb.cancel_remaining_commands()
        self.onAbort()
        time.sleep(0.5)
        self.onResume()
        print(" on restart server ")
        epv = self.scandb.get_info('epics_status_prefix', default=None)
        if epv is not None:
            shutdownpv = epics.PV(epv + 'Shutdown')
            time.sleep(.1)
            print("Shutdown PV ", shutdownpv)
            shutdownpv.put(1)

    def onClose(self, event=None):
        self.SaveMacroFile(AUTOSAVE_FILE)
        self._stimer.Stop()

        time.sleep(0.25)
        self.input.SaveHistory(self.histfile)
        time.sleep(0.25)
        self.Destroy()
