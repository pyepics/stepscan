import os, sys, time
import json
import glob
import numpy as np
import h5py
try:
    from pyFAI.azimuthalIntegrator import AzimuthalIntegrator
    HAS_PYFAI = True
except ImportError:
    HAS_PYFAI = False

MAXVAL = 2**32 - 2**15

class AD_Integrator(object):
    """1D integrator"""
    def __init__(self,  suffix='h5', mask=None, **kws):
        from epicsscan.scandb import ScanDB
        self.scandb = ScanDB()
        self.folder = ''
        self.label = ''
        self.mask = mask
        self.suffix = suffix
        self.sleep_time = 1.0
        self.set_state('idle')

    def set_state(self, state):
        self.scandb.set_info('xrd_1dint_status', state.lower())

    def get_state(self):
        return self.scandb.get_info('xrd_1dint_status').lower()

    def read_config(self):
        calfile = self.scandb.get_info('xrd_calibration')
        self.label = self.scandb.get_info('xrd_1dint_label')
        self.folder = self.scandb.get_info('map_folder')
        if self.folder.endswith('/'):
            self.folder = self.folder[:-1]

        calib = json.loads(self.scandb.get_detectorconfig(calfile).text)
        print("Read Integration configuration: ", calfile)
        if HAS_PYFAI:
            self.integrator = AzimuthalIntegrator(**calib)

    def save_1dint(self, h5file, outfile):
        t0 = time.time()
        if not HAS_PYFAI:
            return
        try:
            xrdfile = h5py.File(h5file, 'r')
        except IOError:
            time.sleep(2.0*self.sleep_time)
            return
        data = xrdfile['/entry/data/data']
        if self.mask is not None:
            data = data * self.mask
        if data.shape[1] > data.shape[2]:
            data = data[:, 3:-3, 1:-1]
        else:
            data = data[:, 1:-1, 3:-3]

        nframes, nx, ny = data.shape
        xrdfile.close()
        integrate = self.integrator.integrate1d
        opts = dict(method='csr',unit='q_A^-1',
                    correctSolidAngle=True,
                    polarization_factor=0.999)
        dat = []
        for i in range(nframes):
            img = data[i, :, :]
            img[np.where(img>MAXVAL)] = 0
            q, x = integrate(img[::-1, :], 2048, **opts)
            if i == 0:
                dat.append(q)
            dat.append(x)
        dat = np.array(dat)
        _path, fname = os.path.split(outfile)
        print("writing 1D data: %s, %.2f sec" %  (fname, time.time()-t0))
        np.save(outfile, dat)

    def integrate(self):
        if len(self.folder) < 0:
            self.read_config()
        fname = '%s*.%s' % (self.label, self.suffix)
        xrdfiles = glob.glob(os.path.join(self.folder, fname))
        for xfile in sorted(xrdfiles):
            outfile = xfile.replace(self.suffix, 'npy')
            if not os.path.exists(outfile):
                self.save_1dint(xfile, outfile)

    def run(self):
        while True:
            time.sleep(self.sleep_time)
            state = self.get_state()
            # print(state, self.folder)
            if state.startswith('starting'):
                self.read_config()
                self.set_state('running')
            elif state.startswith('running'):
                self.integrate()
            elif state.startswith('finishing'):
                self.integrate()
                self.set_state('idle')
                self.folder = ''
            elif state.startswith('idle'):
                time.sleep(5*self.sleep_time)
            elif state.startswith('quit'):
                return


def read_poni(fname):
    """read XRD calibration from pyFAI poni file"""
    conf = dict(dist=None, wavelength=None, pixel1=None, pixel2=None,
                poni1=None, poni2=None, rot1=None, rot2=None, rot3=None)

    with open(fname, 'r') as fh:
        for line in fh.readlines():
            line = line[:-1].strip()
            if line.startswith('#'):
                continue
            key, val = [a.strip() for a in line.split(':', 1)]
            key = key.lower()
            if key == 'detector_config':
                confdict = json.loads(val)
                for k, v in confdict.items():
                    k = k.lower()
                    if k in conf:
                        conf[k] = float(v)

            else:
                if key == 'distance':
                    key='dist'
                elif key == 'pixelsize1':
                    key='pixel1'
                elif key == 'pixelsize2':
                    key='pixel2'
                if key in conf:
                    conf[key] = float(val)
    missing = []
    for key, val in conf.items():
        if val is None:
            missing.append(key)
    if len(missing)>0:
        msg = "'%s' is not a valid PONI file: missing '%s'"
        raise ValueError(msg % (fname, ', '.join(missing)))
    return conf
