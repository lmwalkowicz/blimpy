#!/usr/bin/env python
"""
# filterbank.py

Python class and command line utility for reading and plotting filterbank files.

This provides a class, Filterbank(), which can be used to read a .fil file:

    ````
    fil = Filterbank('test_psr.fil')
    print fil.header
    print fil.data.shape
    print fil.freqs

    plt.figure()
    fil.plot_spectrum(t=0)
    plt.show()
    ````

TODO: check the file seek logic works correctly for multiple IFs

"""

import os
import sys
import struct
import numpy as np
from pprint import pprint

from astropy import units as u
from astropy.coordinates import Angle
import scipy.stats
from matplotlib.ticker import NullFormatter

from utils import db, lin, rebin, closest

try:
    import h5py
    HAS_HDF5 = True
except ImportError:
    HAS_HDF5 = False

# Check if $DISPLAY is set (for handling plotting on remote machines with no X-forwarding)
if os.environ.has_key('DISPLAY'):
    import pylab as plt
else:
    import matplotlib
    matplotlib.use('Agg')
    import pylab as plt


from .sigproc_header import *

###
# Config values
###

MAX_PLT_POINTS      = 65536                  # Max number of points in matplotlib plot
MAX_IMSHOW_POINTS   = (8192, 4096)           # Max number of points in imshow plot
MAX_DATA_ARRAY_SIZE = 1024 * 1024 * 1024     # Max size of data array to load into memory
MAX_HEADER_BLOCKS   = 100                    # Max size of header (in 512-byte blocks)


###
# Main blimpy class
###

class Filterbank(object):
    """ Class for loading and plotting blimpy data """

    def __repr__(self):
        return "Filterbank data: %s" % self.filename

    def __init__(self, filename=None, f_start=None, f_stop=None,
                 t_start=None, t_stop=None, load_data=True,
                 header_dict=None, data_array=None):
        """ Class for loading and plotting blimpy data.

        This class parses the blimpy file and stores the header and data
        as objects:
            fb = Filterbank('filename_here.fil')
            fb.header        # blimpy header, as a dictionary
            fb.data          # blimpy data, as a numpy array

        Args:
            filename (str): filename of blimpy file.
            f_start (float): start frequency in MHz
            f_stop (float): stop frequency in MHz
            t_start (int): start integration ID
            t_stop (int): stop integration ID
            load_data (bool): load data. If set to False, only header will be read.
            header_dict (dict): Create blimpy from header dictionary + data array
            data_array (np.array): Create blimpy from header dict + data array
        """

        if filename:
            self.filename = filename
            if HAS_HDF5:
                if h5py.is_hdf5(filename):
                    self.read_hdf5(filename, f_start, f_stop, t_start, t_stop, load_data)
                else:
                    self.read_filterbank(filename, f_start, f_stop, t_start, t_stop, load_data)
            else:
                self.read_filterbank(filename, f_start, f_stop, t_start, t_stop, load_data)
        elif header_dict is not None and data_array is not None:
            self.gen_from_header(header_dict, data_array)
        else:
            pass

    def gen_from_header(self, header_dict, data_array, f_start=None, f_stop=None,
                        t_start=None, t_stop=None, load_data=True):
        self.filename = ''
        self.header = header_dict
        self.data = data_array
        self.n_ints_in_file = 0

        self._setup_freqs()

    def read_hdf5(self, filename, f_start=None, f_stop=None,
                        t_start=None, t_stop=None, load_data=True):
        self.header = {}
        self.filename = filename
        self.h5 = h5py.File(filename)
        for key, val in self.h5['data'].attrs.items():
            if key == 'src_raj':
                self.header[key] = Angle(val, unit='hr')
            elif key == 'src_dej':
                self.header[key] = Angle(val, unit='deg')
            else:
                self.header[key] = val

        self.data = self.h5["data"][:]
        self._setup_freqs()

        self.n_ints_in_file  = self.data.shape[0]
        self.file_size_bytes = os.path.getsize(self.filename)

    def _setup_freqs(self, f_start=None, f_stop=None):
        ## Setup frequency axis
        f0 = self.header['fch1']
        f_delt = self.header['foff']

        i_start, i_stop = 0, self.header['nchans']
        if f_start:
            i_start = (f_start - f0) / f_delt
        if f_stop:
            i_stop  = (f_stop - f0)  / f_delt

        #calculate closest true index value
        chan_start_idx = np.int(i_start)
        chan_stop_idx  = np.int(i_stop)

        #create freq array
        if i_start < i_stop:
            i_vals = np.arange(chan_start_idx, chan_stop_idx)
        else:
            i_vals = np.arange(chan_stop_idx, chan_start_idx)

        self.freqs = f_delt * i_vals + f0

        if f_delt < 0:
            self.freqs = self.freqs[::-1]

        return i_start, i_stop, chan_start_idx, chan_stop_idx

    def __setup_time_axis(self,t_start=None, t_stop=None):
        """  Setup time axis.
        """

        # now check to see how many integrations requested
        ii_start, ii_stop = 0, self.n_ints_in_file
        if t_start:
            ii_start = t_start
        if t_stop:
            ii_stop = t_stop
        n_ints = ii_stop - ii_start

        ## Setup time axis
        t0 = self.header['tstart']
        t_delt = self.header['tsamp']

    def read_filterbank(self, filename=None, f_start=None, f_stop=None,
                        t_start=None, t_stop=None, load_data=True):

        if filename is None:
            filename = self.filename

        self.header = read_header(filename)

        ## Setup frequency axis
        f0 = self.header['fch1']
        f_delt = self.header['foff']

        # keep this seperate!
        # file_freq_mapping =  np.arange(0, self.header['nchans'], 1, dtype='float64') * f_delt + f0

        #convert input frequencies into what their corresponding index would be

        i_start, i_stop, chan_start_idx, chan_stop_idx = self._setup_freqs(f_start, f_stop)

        n_bytes  = self.header['nbits'] / 8
        n_chans = self.header['nchans']
        n_chans_selected = self.freqs.shape[0]
        n_ifs   = self.header['nifs']


        # Load binary data
        self.idx_data = len_header(filename)
        f = open(filename, 'rb')
        f.seek(self.idx_data)
        filesize = os.path.getsize(self.filename)
        n_bytes_data = filesize - self.idx_data
        n_ints_in_file = n_bytes_data / (n_bytes * n_chans * n_ifs)

        # now check to see how many integrations requested
        ii_start, ii_stop = 0, n_ints_in_file
        if t_start:
            ii_start = t_start
        if t_stop:
            ii_stop = t_stop
        n_ints = ii_stop - ii_start

        # Seek to first integration
        f.seek(ii_start * n_bytes * n_ifs * n_chans, 1)

        # Set up indexes used in file read (taken out of loop for speed)
        i0 = np.min((chan_start_idx, chan_stop_idx))
        i1 = np.max((chan_start_idx, chan_stop_idx))

        #Set up the data type (taken out of loop for speed)
        if n_bytes == 4:
            dd_type = 'float32'
        elif n_bytes == 2:
            dd_type = 'int16'
        elif n_bytes == 1:
            dd_type = 'int8'

        if load_data:

            if n_ints * n_ifs * n_chans_selected > MAX_DATA_ARRAY_SIZE:
                print "Error: data array is too large to load. Either select fewer"
                print "points or manually increase MAX_DATA_ARRAY_SIZE."
                exit()

            self.data = np.zeros((n_ints, n_ifs, n_chans_selected), dtype='float32')

            for ii in range(n_ints):
                """d = f.read(n_bytes * n_chans * n_ifs)
                """

                for jj in range(n_ifs):

                    f.seek(n_bytes * i0, 1) # 1 = from current location
                    #d = f.read(n_bytes * n_chans_selected)
                    #bytes_to_read = n_bytes * n_chans_selected

                    dd = np.fromfile(f, count=n_chans_selected, dtype=dd_type)

                    # Reverse array if frequency axis is flipped
                    if f_delt < 0:
                        dd = dd[::-1]

                    self.data[ii, jj] = dd

                    f.seek(n_bytes * (n_chans - i1), 1)  # Seek to start of next block
        else:
            print "Skipping data load..."
            self.data = np.array([0])

        # Finally add some other info to the class as objects
        self.n_ints_in_file  = n_ints_in_file
        self.file_size_bytes = filesize

        ## Setup time axis
        t0 = self.header['tstart']
        t_delt = self.header['tsamp']
        self.timestamps = np.arange(0, n_ints) * t_delt / 24./60./60 + t0

    def blank_dc(self, n_coarse_chan):
        """ Blank DC bins in coarse channels.

        Note: currently only works if entire blimpy file is read
        """
        n_chan = self.data.shape[2]
        n_chan_per_coarse = n_chan / n_coarse_chan

        mid_chan = n_chan_per_coarse / 2

        for ii in range(0, n_coarse_chan-1):
            ss = ii*n_chan_per_coarse
            self.data[..., ss+mid_chan-1] = self.data[..., ss+mid_chan]

    def info(self):
        """ Print header information """

        for key, val in self.header.items():
            if key == 'src_raj':
                val = val.to_string(unit=u.hour, sep=':')
            if key == 'src_dej':
                val = val.to_string(unit=u.deg, sep=':')
            print "%16s : %32s" % (key, val)

        print "\n%16s : %32s" % ("Num ints in file", self.n_ints_in_file)
        print "%16s : %32s" % ("Data shape", self.data.shape)
        print "%16s : %32s" % ("Start freq (MHz)", self.freqs[0])
        print "%16s : %32s" % ("Stop freq (MHz)", self.freqs[-1])

    def generate_freqs(self, f_start, f_stop):
        """
        returns frequency array [f_start...f_stop]
        """

        fch1 = self.header['fch1']
        foff = self.header['foff']

        #convert input frequencies into what their corresponding index would be
        i_start = (f_start - fch1) / foff
        i_stop  = (f_stop - fch1)  / foff

        #calculate closest true index value
        chan_start_idx = np.int(i_start)
        chan_stop_idx  = np.int(i_stop)

        #create freq array
        i_vals = np.arange(chan_stop_idx, chan_start_idx, 1)

        freqs = foff * i_vals + fch1

        return freqs[::-1]

    def grab_data(self, f_start=None, f_stop=None, if_id=0):
        """ Extract a portion of data by frequency range.

        Args:
            f_start (float): start frequency in MHz
            f_stop (float): stop frequency in MHz
            if_id (int): IF input identification (req. when multiple IFs in file)

        Returns:
            (freqs, data) (np.arrays): frequency axis in MHz and data subset
        """
        i_start, i_stop = 0, None

        if f_start:
            i_start = closest(self.freqs, f_start)
        if f_stop:
            i_stop = closest(self.freqs, f_stop)

        plot_f    = self.freqs[i_start:i_stop]
        plot_data = self.data[:, if_id, i_start:i_stop]
        return plot_f, plot_data

    def calc_n_coarse_chan(self):
        ''' This makes an attempt to calculate the number of coarse channels in a given file.
            It assumes for now that a single coarse channel is 2.9296875 MHz
        '''

        # Could add a telescope based coarse channel bandwidth, or other discriminative.
        # if telescope_id == 'GBT':
        # or actually as is currently
        # if self.header['telescope_id'] == 6:

        coarse_chan_bw = 2.9296875

        bandwidth = abs(self.header['nchans']*self.header['foff'])
        n_coarse_chan = int(bandwidth / coarse_chan_bw)

        return max(n_coarse_chan, 1)

    def plot_spectrum(self, t=0, f_start=None, f_stop=None, logged=False, if_id=0, c=None, **kwargs):
        """ Plot frequency spectrum of a given file

        Args:
            t (int): integration number to plot (0 -> len(data))
            logged (bool): Plot in linear (False) or dB units (True)
            if_id (int): IF identification (if multiple IF signals in file)
            c: color for line
            kwargs: keyword args to be passed to matplotlib plot()
        """
        ax = plt.gca()

        plot_f, plot_data = self.grab_data(f_start, f_stop, if_id)

        if isinstance(t, int):
            print "extracting integration %i..." % t
            plot_data = plot_data[t]
        elif t == 'all':
            print "averaging along time axis..."
            plot_data = plot_data.mean(axis=0)
        else:
            raise RuntimeError("Unknown integration %s" % t)

        # Rebin to max number of points
        dec_fac_x = 1
        if plot_data.shape[0] > MAX_PLT_POINTS:
            dec_fac_x = plot_data.shape[0] / MAX_PLT_POINTS

        plot_data = rebin(plot_data, dec_fac_x, 1)
        plot_f    = rebin(plot_f, dec_fac_x, 1)

        if not c:
            kwargs['c'] = '#333333'

        if logged:
            plt.plot(plot_f, db(plot_data),label='Stokes I', **kwargs)
            plt.ylabel("Power [dB]")
        else:

            plt.plot(plot_f, plot_data,label='Stokes I', **kwargs)
            plt.ylabel("Power [counts]")
        plt.xlabel("Frequency [MHz]")
        plt.legend()

        try:
            plt.title(self.header['source_name'])
        except KeyError:
            plt.title(self.filename)

        ax.get_xaxis().get_major_formatter().set_useOffset(False)
        plt.xlim(plot_f[0], plot_f[-1])

    def plot_spectrum_min_max(self, t=0, f_start=None, f_stop=None, logged=False, if_id=0, c=None, **kwargs):
        """ Plot frequency spectrum of a given file

        Args:
            logged (bool): Plot in linear (False) or dB units (True)
            if_id (int): IF identification (if multiple IF signals in file)
            c: color for line
            kwargs: keyword args to be passed to matplotlib plot()
        """
        ax = plt.gca()

        plot_f, plot_data = self.grab_data(f_start, f_stop, if_id)

        fig_max = plot_data[0].max()
        fig_min = plot_data[0].min()

        print "averaging along time axis..."
        plot_max = plot_data.max(axis=0)
        plot_min = plot_data.min(axis=0)
        plot_data = plot_data.mean(axis=0)

        # Rebin to max number of points
        dec_fac_x = 1
        MAX_PLT_POINTS = 8*64  # Low resoluition to see the difference.
        if plot_data.shape[0] > MAX_PLT_POINTS:
            dec_fac_x = plot_data.shape[0] / MAX_PLT_POINTS

        plot_data = rebin(plot_data, dec_fac_x, 1)
        plot_min = rebin(plot_min, dec_fac_x, 1)
        plot_max = rebin(plot_max, dec_fac_x, 1)
        plot_f    = rebin(plot_f, dec_fac_x, 1)

        if logged:
            plt.plot(plot_f, db(plot_data), "#333333", label='mean', **kwargs)
            plt.plot(plot_f, db(plot_max),  "#e74c3c", label='max', **kwargs)
            plt.plot(plot_f, db(plot_min),  '#3b5b92', label='min', **kwargs)
            plt.ylabel("Power [dB]")
        else:
            plt.plot(plot_f, plot_data,  "#333333", label='mean', **kwargs)
            plt.plot(plot_f, plot_max,   "#e74c3c", label='max', **kwargs)
            plt.plot(plot_f, plot_min,   '#3b5b92', label='min', **kwargs)
            plt.ylabel("Power [counts]")
        plt.xlabel("Frequency [MHz]")
        plt.legend()

        try:
            plt.title(self.header['source_name'])
        except KeyError:
            plt.title(self.filename)

        ax.get_xaxis().get_major_formatter().set_useOffset(False)
        plt.xlim(plot_f[0], plot_f[-1])
        plt.ylim(db(fig_min),db(fig_max))

    def plot_waterfall(self, f_start=None, f_stop=None, if_id=0, logged=True,cb=True,MJD_time=False, **kwargs):
        """ Plot waterfall of data

        Args:
            f_start (float): start frequency, in MHz
            f_stop (float): stop frequency, in MHz
            logged (bool): Plot in linear (False) or dB units (True),
            cb (bool): for plotting the colorbar
            kwargs: keyword args to be passed to matplotlib imshow()
        """
        plot_f, plot_data = self.grab_data(f_start, f_stop, if_id)

        if logged:
            plot_data = db(plot_data)

        # Make sure waterfall plot is under 4k*4k
        dec_fac_x, dec_fac_y = 1, 1
        if plot_data.shape[0] > MAX_IMSHOW_POINTS[0]:
            dec_fac_x = plot_data.shape[0] / MAX_IMSHOW_POINTS[0]

        if plot_data.shape[1] > MAX_IMSHOW_POINTS[1]:
            dec_fac_y =  plot_data.shape[1] /  MAX_IMSHOW_POINTS[1]

        plot_data = rebin(plot_data, dec_fac_x, dec_fac_y)

        try:
            plt.title(self.header['source_name'])
        except KeyError:
            plt.title(self.filename)

        if MJD_time:
            extent=(plot_f[0], plot_f[-1], self.timestamps[-1], self.timestamps[0])
        else:
            extent=(plot_f[0], plot_f[-1], (self.timestamps[-1]-self.timestamps[0])*24.*60.*60, 0.0)

        plt.imshow(plot_data,
            aspect='auto',
            rasterized=True,
            interpolation='nearest',
            extent=extent,
            cmap='viridis',
            **kwargs
        )
        if cb:
            plt.colorbar()
        plt.xlabel("Frequency [MHz]")
        if MJD_time:
            plt.ylabel("Time [MJD]")
        else:
            plt.ylabel("Time from tstart [s]")

    def plot_time_series(self, f_start=None, f_stop=None, if_id=0, logged=True, orientation=None , **kwargs):
        """ Plot the time series.

         Args:
            f_start (float): start frequency, in MHz
            f_stop (float): stop frequency, in MHz
            logged (bool): Plot in linear (False) or dB units (True),
            kwargs: keyword args to be passed to matplotlib imshow()
        """

        ax = plt.gca()
        plot_f, plot_data = self.grab_data(f_start, f_stop, if_id)

        if logged:
            plot_data = db(plot_data)

        plot_data = plot_data.mean(axis=1)

        if 'v' in orientation:
            plt.plot(plot_data,range(len(plot_data))[::-1], **kwargs)
        else:
            plt.plot(plot_data, **kwargs)
            plt.xlabel("Time [s]")

        ax.autoscale(axis='both',tight=True)
        ax.get_xaxis().get_major_formatter().set_useOffset(False)

    def plot_kurtosis(self, f_start=None, f_stop=None, if_id=0, **kwargs):
        """ Plot kurtosis

         Args:
            f_start (float): start frequency, in MHz
            f_stop (float): stop frequency, in MHz
            kwargs: keyword args to be passed to matplotlib imshow()
        """
        ax = plt.gca()

        plot_f, plot_data = self.grab_data(f_start, f_stop, if_id)

        try:
            plot_kurtosis = scipy.stats.kurtosis(plot_data, axis=0, nan_policy='omit')
        except:
            plot_kurtosis = plot_data*0.0

        plt.plot(plot_f, plot_kurtosis, **kwargs)
        plt.ylabel("Kurtosis")
        plt.xlabel("Frequency [MHz]")

        ax.get_xaxis().get_major_formatter().set_useOffset(False)
        plt.xlim(plot_f[0], plot_f[-1])

    def plot_all(self, t=0, f_start=None, f_stop=None, logged=False, if_id=0, kutosis=True, **kwargs):
        """ Plot waterfall of data as well as spectrum; also, placeholder to make even more complicated plots in the future.

        Args:
            f_start (float): start frequency, in MHz
            f_stop (float): stop frequency, in MHz
            logged (bool): Plot in linear (False) or dB units (True),
            t (int): integration number to plot (0 -> len(data))
            logged (bool): Plot in linear (False) or dB units (True)
            if_id (int): IF identification (if multiple IF signals in file)
            kwargs: keyword args to be passed to matplotlib plot() and imshow()
        """

        plot_f, plot_data = self.grab_data(f_start, f_stop, if_id)

        nullfmt = NullFormatter()  # no labels

        # definitions for the axes
        left, width = 0.35, 0.5
        bottom, height = 0.45, 0.5
        width2, height2 = 0.1125, 0.15
        bottom2, left2 = bottom - height2 - .025, left - width2 - .02
        bottom3, left3 = bottom2 - height2 - .025, 0.075

        rect_waterfall = [left, bottom, width, height]
        rect_colorbar = [left + width, bottom, .025, height]
        rect_spectrum = [left, bottom2, width, height2]
        rect_min_max = [left, bottom3, width, height2]
        rect_timeseries = [left + width, bottom, width2, height]
        rect_kurtosis = [left3, bottom3, 0.25, height2]
        rect_header = [left3 - .05, bottom, 0.2, height]

        # --------
        axWaterfall = plt.axes(rect_waterfall)
        print 'Ploting Waterfall'
        self.plot_waterfall(f_start=f_start, f_stop=f_stop, cb=False)
        plt.xlabel('')

        # no labels
        axWaterfall.xaxis.set_major_formatter(nullfmt)

        # --------
        #         axColorbar = plt.axes(rect_colorbar)
        #         print 'Ploting Colorbar'
        #         print plot_data.max()
        #         print plot_data.min()
        #
        #         plot_colorbar = range(plot_data.min(),plot_data.max(),int((plot_data.max()-plot_data.min())/plot_data.shape[0]))
        #         plot_colorbar = np.array([[plot_colorbar],[plot_colorbar]])
        #
        #         plt.imshow(plot_colorbar,aspect='auto', rasterized=True, interpolation='nearest',)

        #         axColorbar.xaxis.set_major_formatter(nullfmt)
        #         axColorbar.yaxis.set_major_formatter(nullfmt)

        #         heatmap = axColorbar.pcolor(plot_data, edgecolors = 'none', picker=True)
        #         plt.colorbar(heatmap, cax = axColorbar)

        # --------
        axSpectrum = plt.axes(rect_spectrum)
        print 'Ploting Spectrum'
        self.plot_spectrum(logged=logged, f_start=f_start, f_stop=f_stop, t=t)
        plt.title('')
        axSpectrum.yaxis.tick_right()
        axSpectrum.yaxis.set_label_position("right")
        plt.xlabel('')
        axSpectrum.xaxis.set_major_formatter(nullfmt)

        # --------
        axTimeseries = plt.axes(rect_timeseries)
        print 'Plotting Timeseries'
        self.plot_time_series(f_start=f_start, f_stop=f_stop, orientation='v')
        axTimeseries.yaxis.set_major_formatter(nullfmt)
        axTimeseries.xaxis.set_major_formatter(nullfmt)

        # --------
        # Could exclude since it takes much longer to run than the other plots.
        if kutosis:
            axKurtosis = plt.axes(rect_kurtosis)
            print 'Plotting Kurtosis'
            self.plot_kurtosis(f_start=f_start, f_stop=f_stop)

        # --------
        axMinMax = plt.axes(rect_min_max)
        print 'Plotting Min Max'
        self.plot_spectrum_min_max(logged=logged, f_start=f_start, f_stop=f_stop, t=t)
        plt.title('')
        axMinMax.yaxis.tick_right()
        axMinMax.yaxis.set_label_position("right")

        # --------
        axHeader = plt.axes(rect_header)
        print 'Plotting Header'
        # Generate nicer header
        telescopes = {0: 'Fake data',
                      1: 'Arecibo',
                      2: 'Ooty',
                      3: 'Nancay',
                      4: 'Parkes',
                      5: 'Jodrell',
                      6: 'GBT',
                      8: 'Effelsberg',
                      10: 'SRT',
                      64: 'MeerKAT',
                      65: 'KAT7'
                      }

        telescope = telescopes.get(self.header["telescope_id"], self.header["telescope_id"])

        plot_header = "%14s: %s\n" % ("TELESCOPE_ID", telescope)
        for key in ('SRC_RAJ', 'SRC_DEJ', 'TSTART', 'NCHANS', 'NBEAMS', 'NIFS', 'NBITS'):
            try:
                plot_header += "%14s: %s\n" % (key, self.header[key.lower()])
            except KeyError:
                pass
        fch1 = "%6.6f MHz" % self.header['fch1']

        foff = (self.header['foff'] * 1e6 * u.Hz)
        if np.abs(foff) > 1e6 * u.Hz:
            foff = str(foff.to('MHz'))
        elif np.abs(foff) > 1e3 * u.Hz:
            foff = str(foff.to('kHz'))
        else:
            foff = str(foff.to('Hz'))

        plot_header += "%14s: %s\n" % ("FCH1", fch1)
        plot_header += "%14s: %s\n" % ("FOFF", foff)

        plt.text(0.05, .95, plot_header, ha='left', va='top', wrap=True)

        axHeader.set_axis_bgcolor('white')
        axHeader.xaxis.set_major_formatter(nullfmt)
        axHeader.yaxis.set_major_formatter(nullfmt)

    def write_to_filterbank(self, filename_out):
        """ Write data to blimpy file.

        Args:
            filename_out (str): Name of output file
        """

        # calibrate data
        # self.data = calibrate(mask(self.data.mean(axis=0)[0]))
        # rewrite header to be consistent with modified data
        self.header['fch1']   = self.freqs[0]
        self.header['foff']   = self.freqs[1] - self.freqs[0]
        self.header['nchans'] = self.freqs.shape[0]
        # self.header['tsamp']  = self.data.shape[0] * self.header['tsamp']

        n_bytes  = self.header['nbits'] / 8
        with open(filename_out, "w") as fileh:
            fileh.write(generate_sigproc_header(self))
            j = self.data
            if n_bytes == 4:
                np.float32(j[:, ::-1].ravel()).tofile(fileh)
            elif n_bytes == 2:
                np.int16(j[:, ::-1].ravel()).tofile(fileh)
            elif n_bytes == 1:
                np.int8(j[:, ::-1].ravel()).tofile(fileh)

    def write_to_hdf5(self, filename_out, *args, **kwargs):
        """ Write data to HDF5 file.

        Args:
            filename_out (str): Name of output file
        """
        if not HAS_HDF5:
            raise RuntimeError("h5py package required for HDF5 output.")

        with h5py.File(filename_out, 'w') as h5:

            dset = h5.create_dataset('data',
                              data=self.data,
                              compression='lzf')

            dset_mask = h5.create_dataset('mask',
                                     shape=self.data.shape,
                                     compression='lzf',
                                     dtype='uint8')

            dset.dims[0].label = "frequency"
            dset.dims[1].label = "feed_id"
            dset.dims[2].label = "time"

            dset_mask.dims[0].label = "frequency"
            dset_mask.dims[1].label = "feed_id"
            dset_mask.dims[2].label = "time"

            # Copy over header information as attributes
            for key, value in self.header.items():
                dset.attrs[key] = value


def cmd_tool(args=None):
    """ Command line tool for plotting and viewing info on filterbank files """

    from argparse import ArgumentParser

    parser = ArgumentParser(description="Command line utility for reading and plotting filterbank files.")

    parser.add_argument('-p', action='store',  default='a', dest='what_to_plot', type=str,
                        help='Show: "w" waterfall (freq vs. time) plot; "s" integrated spectrum plot, \
                             "a" for all available plots and information; and more.')
    parser.add_argument('filename', type=str,
                        help='Name of file to read')
    parser.add_argument('-b', action='store', default=None, dest='f_start', type=float,
                        help='Start frequency (begin), in MHz')
    parser.add_argument('-e', action='store', default=None, dest='f_stop', type=float,
                        help='Stop frequency (end), in MHz')
    parser.add_argument('-B', action='store', default=None, dest='t_start', type=int,
                        help='Start integration (begin) ID')
    parser.add_argument('-E', action='store', default=None, dest='t_stop', type=int,
                        help='Stop integration (end) ID')
    parser.add_argument('-i', action='store_true', default=False, dest='info_only',
                        help='Show info only')
    parser.add_argument('-a', action='store_true', default=False, dest='average',
                       help='average along time axis (plot spectrum only)')
    parser.add_argument('-s', action='store', default='', dest='plt_filename', type=str,
                       help='save plot graphic to file (give filename as argument)')
    parser.add_argument('-S', action='store_true', default=False, dest='save_only',
                       help='Turn off plotting of data and only save to file.')
    parser.add_argument('-D', action='store_false', default=True, dest='blank_dc',
                       help='Use to not blank DC bin.')
    args = parser.parse_args()

    # Open blimpy data
    filename = args.filename
    load_data = not args.info_only

    # only load one integration if looking at spectrum
    wtp = args.what_to_plot
    if not wtp or 's' in wtp:
        if args.t_start == None:
            t_start = 0
        else:
            t_start = args.t_start
        t_stop  = t_start + 1

        if args.average:
            t_start = None
            t_stop  = None
    else:
        t_start = args.t_start
        t_stop  = args.t_stop

    fil = Filterbank(filename, f_start=args.f_start, f_stop=args.f_stop,
                     t_start=t_start, t_stop=t_stop,
                     load_data=load_data)
    fil.info()

    # And if we want to plot data, then plot data.
    if not args.info_only:
        # check start & stop frequencies make sense
        #try:
        #    if args.f_start:
        #        print "Start freq: %2.2f" % args.f_start
        #        assert args.f_start >= fil.freqs[0] or np.isclose(args.f_start, fil.freqs[0])
        #
        #    if args.f_stop:
        #        print "Stop freq: %2.2f" % args.f_stop
        #        assert args.f_stop <= fil.freqs[-1] or np.isclose(args.f_stop, fil.freqs[-1])
        #except AssertionError:
        #    print "Error: Start and stop frequencies must lie inside file's frequency range."
        #    print "i.e. between %2.2f-%2.2f MHz." % (fil.freqs[0], fil.freqs[-1])
        #    exit()

        if args.blank_dc:
            print "Blanking DC bin"
            n_coarse_chan = fil.calc_n_coarse_chan()
            fil.blank_dc(n_coarse_chan)

        if args.what_to_plot == "w":
            plt.figure("waterfall", figsize=(8, 6))
            fil.plot_waterfall(f_start=args.f_start, f_stop=args.f_stop)
        elif args.what_to_plot == "s":
            plt.figure("Spectrum", figsize=(8, 6))
            fil.plot_spectrum(logged=True, f_start=args.f_start, f_stop=args.f_stop, t='all')
        elif args.what_to_plot == "mm":
            plt.figure("min max", figsize=(8, 6))
            fil.plot_spectrum_min_max(logged=True, f_start=args.f_start, f_stop=args.f_stop, t='all')
        elif args.what_to_plot == "k":
            plt.figure("kurtosis", figsize=(8, 6))
            fil.plot_kurtosis(f_start=args.f_start, f_stop=args.f_stop)
        elif args.what_to_plot == "t":
            plt.figure("Time Series", figsize=(8, 6))
            fil.plot_time_series(f_start=args.f_start, f_stop=args.f_stop)
        elif args.what_to_plot == "a":
            plt.figure("Multiple diagnostic plots", figsize=(12, 9),facecolor='white')
            fil.plot_all(logged=True, f_start=args.f_start, f_stop=args.f_stop, t='all')
        elif args.what_to_plot == "ank":
            plt.figure("Multiple diagnostic plots", figsize=(12, 9),facecolor='white')
            fil.plot_all(logged=True, f_start=args.f_start, f_stop=args.f_stop, t='all',kutosis=False)

        if args.plt_filename != '':
            plt.savefig(args.plt_filename)

        if not args.save_only:
            if os.environ.has_key('DISPLAY'):
                plt.show()
            else:
                print "No $DISPLAY available."


if __name__ == "__main__":
    cmd_tool()
