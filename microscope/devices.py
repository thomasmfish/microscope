#!/usr/bin/env python
# -*- coding: utf-8 -*-

## Copyright (C) 2017 David Pinto <david.pinto@bioch.ox.ac.uk>
## Copyright (C) 2016 Mick Phillips <mick.phillips@gmail.com>
##
## Microscope is free software: you can redistribute it and/or modify
## it under the terms of the GNU General Public License as published by
## the Free Software Foundation, either version 3 of the License, or
## (at your option) any later version.
##
## Microscope is distributed in the hope that it will be useful,
## but WITHOUT ANY WARRANTY; without even the implied warranty of
## MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
## GNU General Public License for more details.
##
## You should have received a copy of the GNU General Public License
## along with Microscope.  If not, see <http://www.gnu.org/licenses/>.

"""Classes for control of microscope components.

This module provides base classes for experiment control and data
acquisition devices that can be served over Pyro. This means that each
device may be served from a separate process, or even from a different PC.
"""

import abc
import functools
import itertools
import logging
import time
from ast import literal_eval
from collections import OrderedDict
from threading import Thread
import threading
import Pyro4
import numpy
import queue

from enum import Enum, EnumMeta

import numpy

from collections import namedtuple
# A tuple that defines a region of interest.
ROI = namedtuple('ROI', ['left', 'top', 'width', 'height'])
# A tuple containing parameters for horizontal and vertical binning.
Binning = namedtuple('Binning', ['h', 'v'])


# Trigger types.
(TRIGGER_AFTER, TRIGGER_BEFORE, TRIGGER_DURATION, TRIGGER_SOFT) = range(4)

# Mapping of setting data types to descriptors allowed-value description types.
# For python 2 and 3 compatibility, we convert the type into a descriptor string.
# This avoids problems with, say a python 2 client recognising a python 3
# <class 'int'> as a python 2 <type 'int'>.
DTYPES = {'int': ('int', tuple),
          'float': ('float', tuple),
          'bool': ('bool', type(None)),
          'enum': ('enum', list, EnumMeta, dict, tuple),
          'str': ('str', int),
          'tuple': ('tuple', type(None)),
          int: ('int', tuple),
          float: ('float', tuple),
          bool: ('bool', type(None)),
          str: ('str', int),
          tuple: ('tuple', type(None))}

# A utility function to call callables or return value of non-callables.
# noinspection PyPep8
_call_if_callable = lambda f: f() if callable(f) else f


class Setting():
    def __init__(self, name, dtype, get_func, set_func=None, values=None, readonly=False):
        """Create a setting.

        :param name: the setting's name
        :param dtype: a data type from ('int', 'float', 'bool', 'enum', 'str')
        :param get_func: a function to get the current value
        :param set_func: a function to set the value
        :param values: a description of allowed values dependent on dtype,
                       or function that returns a description
        :param readonly: an optional flag to indicate a read-only setting.

        A client needs some way of knowing a setting name and data type,
        retrieving the current value and, if settable, a way to retrieve
        allowable values, and set the value.

        Setters and getters accept or return:
            the setting value for int, float, bool and str;
            the setting index into a list, dict or Enum type for enum.
        """
        self.name = name
        if dtype not in DTYPES:
            raise Exception('Unsupported dtype.')
        elif not (isinstance(values, DTYPES[dtype][1:]) or callable(values)):
            raise Exception("Invalid values type for %s '%s': expected function or %s" %
                            (dtype, name, DTYPES[dtype][1:]))
        self.dtype = DTYPES[dtype][0]
        self._get = get_func
        self._values = values
        self._readonly = readonly
        self._last_written = None
        if self._get is not None:
            self._set = set_func
        else:
            # Cache last written value for write-only settings.
            def w(value):
                self._last_written = value
                set_func(value)
            self._set = w

    def describe(self):
        return {  # wrap type in str since can't serialize types
            'type': str(self.dtype),
            'values': self.values(),
            'readonly': self.readonly(),
            'cached': self._last_written is not None}

    def get(self):
        if self._get is not None:
            value = self._get()
        else:
            value = self._last_written
        if isinstance(self._values, EnumMeta):
            return self._values(value).value
        else:
            return value

    def readonly(self):
        return _call_if_callable(self._readonly)

    def set(self, value):
        """Set a setting."""
        if self._set is None:
            raise NotImplementedError
        # TODO further validation.
        if isinstance(self._values, EnumMeta):
            value = self._values(value)
        self._set(value)

    def values(self):
        if isinstance(self._values, EnumMeta):
            return [(v.value, v.name) for v in self._values]
        values = _call_if_callable(self._values)
        if values is not None:
            if self.dtype is 'enum':
                if isinstance(values, dict):
                    return list(values.items())
                else:
                    # self._values is a list or tuple
                    return list(enumerate(values))
            elif self._values is not None:
                return values


def device(cls, host, port, conf={}, uid=None):
    """Define a device and where to serve it.

    A device definition for use in deviceserver config files.

    Args:
        cls (type): type/class of device to serve.
        host (str): hostname or ip address serving the device.
        port (int): port number used to serve the device.
        conf (dict): keyword arguments to construct the device.  The
            device is effectively constructed with `cls(**conf)`.
        uid (str): used to identify "floating" devices (see
            documentation for :class:`FloatingDeviceMixin`)
    """
    return dict(cls=cls, host=host, port=int(port), uid=uid, conf=conf)


class FloatingDeviceMixin(object):
    """A mixin for devices that 'float'.

    Some SDKs handling multiple devices do not allow for explicit
    selection of a specific device: instead, a device must be
    initialized and then queried to determine its ID. This class is
    a mixin which identifies a subclass as floating, and enforces
    the implementation of a 'get_id' method.
    """
    __metaclass__ = abc.ABCMeta

    @abc.abstractmethod
    def get_id(self):
        """Return a unique hardware identifier, such as a serial number."""
        pass


class Device(object):
    """A base device class. All devices should subclass this class.

    Args:
        index (int): the index of the device on a shared library.
            This argument is added by the deviceserver.
    """
    __metaclass__ = abc.ABCMeta

    def __init__(self, index=None):
        self.enabled = None
        # A list of settings. (Can't serialize OrderedDict, so use {}.)
        self.settings = OrderedDict()
        # We fetch a logger here, but it can't log anything until
        # a handler is attached after we've identified this device.
        self._logger = logging.getLogger(self.__class__.__name__)
        self._index = index

    def __del__(self):
        self.shutdown()


    def get_is_enabled(self):
        return self.enabled


    def _on_disable(self):
        """Do any device-specific work on disable.

        Subclasses should override this method, rather than modify
        disable(self).
        """
        return True

    def disable(self):
        """Disable the device for a short period for inactivity."""
        self._on_disable()
        self.enabled = False

    def _on_enable(self):
        """Do any device-specific work on enable.

        Subclasses should override this method, rather than modify
        enable(self).
        """
        return True

    def enable(self):
        """Enable the device."""
        try:
            self.enabled = self._on_enable()
        except Exception as err:
            self._logger.debug("Error in _on_enable:", exc_info=err)

    @abc.abstractmethod
    def _on_shutdown(self):
        """Subclasses over-ride this with tasks to do on shutdown."""
        pass

    @abc.abstractmethod
    def initialize(self):
        """Initialize the device."""
        pass

    def shutdown(self):
        """Shutdown the device for a prolonged period of inactivity."""
        self.disable()
        self._logger.info("Shutting down ... ... ...")
        self._on_shutdown()
        self._logger.info("... ... ... ... shut down completed.")

    def make_safe(self):
        """Put the device into a safe state."""
        pass

    def add_setting(self, name, dtype, get_func, set_func, values, readonly=False):
        """Add a setting definition.

        Can also use self.settings[name] = Setting(name, dtype,...)

        :param name: the setting's name
        :param dtype: a data type from ('int', 'float', 'bool', 'enum', 'str')
        :param get_func: a function to get the current value
        :param set_func: a function to set the value
        :param values: a description of allowed values dependent on dtype,
                       or function that returns a description
        :param readonly: an optional flag to indicate a read-only setting.

        A client needs some way of knowing a setting name and data type,
        retrieving the current value and, if settable, a way to retrieve
        allowable values, and set the value.
        We store this info in an OrderedDict. I considered having a Setting
        class with getter, setter, etc., and adding Setting instances as
        device attributes, but Pyro does not support dot notation to access
        the functions we need (e.g. Device.some_setting.set ), so I'd have to
        write access functions, anyway.
        """
        if dtype not in DTYPES:
            raise Exception('Unsupported dtype.')
        elif not (isinstance(values, DTYPES[dtype][1:]) or callable(values)):
            raise Exception("Invalid values type for %s '%s': expected function or %s" %
                            (dtype, name, DTYPES[dtype][1:]))
        else:
            self.settings[name] = Setting(name, dtype, get_func, set_func, values, readonly)

    def get_setting(self, name):
        """Return the current value of a setting."""
        try:
            return self.settings[name].get()
        except Exception as err:
            self._logger.error("in get_setting(%s):" % (name), exc_info=err)
            raise

    def get_all_settings(self):
        """Return ordered settings as a list of dicts."""
        try:
            return {k: v.get() for k, v in self.settings.items()}
        except Exception as err:
            self._logger.error("in get_all_settings:", exc_info=err)
            raise

    def set_setting(self, name, value):
        """Set a setting."""
        try:
            self.settings[name].set(value)
        except Exception as err:
            self._logger.error("in set_setting(%s):" % (name), exc_info=err)
            raise

    def describe_setting(self, name):
        """Return ordered setting descriptions as a list of dicts."""
        return self.settings[name].describe()

    def describe_settings(self):
        """Return ordered setting descriptions as a list of dicts."""
        return [(k, v.describe()) for (k, v) in self.settings.items()]

    def update_settings(self, incoming, init=False):
        """Update settings based on dict of settings and values."""
        if init:
            # Assume nothing about state: set everything.
            my_keys = set(self.settings.keys())
            their_keys = set(incoming.keys())
            update_keys = my_keys & their_keys
            if update_keys != my_keys:
                missing = ', '.join([k for k in my_keys - their_keys])
                msg = 'update_settings init=True but missing keys: %s.' % missing
                self._logger.debug(msg)
                raise Exception(msg)
        else:
            # Only update changed values.
            my_keys = set(self.settings.keys())
            their_keys = set(incoming.keys())
            update_keys = set(key for key in my_keys & their_keys
                              if self.get_setting(key) != incoming[key])
        results = {}
        # Update values.
        for key in update_keys:
            if key not in my_keys or not self.settings[key].set:
                # Setting not recognised or no set function implemented
                results[key] = NotImplemented
                update_keys.remove(key)
                continue
            if _call_if_callable(self.settings[key].readonly):
                continue
            self.settings[key].set(incoming[key])
        # Read back values in second loop.
        for key in update_keys:
            results[key] = self.settings[key].get()
        return results


def keep_acquiring(func):
    """Wrapper to preserve acquiring state of data capture devices."""
    def wrapper(self, *args, **kwargs):
        if self._acquiring:
            self.abort()
            result = func(self, *args, **kwargs)
            self._on_enable()
        else:
            result = func(self, *args, **kwargs)
        return result

    return wrapper


class DataDevice(Device):
    """A data capture device.

    This class handles a thread to fetch data from a device and dispatch
    it to a client.  The client is set using set_client(uri) or (legacy)
    receiveClient(uri).

    Derived classed should implement::
      * abort(self)                ---  required
      * _fetch_data(self)          ---  required
      * _process_data(self, data)  ---  optional

    Derived classes may override __init__, enable and disable, but must
    ensure to call this class's implementations as indicated in the docstrings.
    """
    __metaclass__ = abc.ABCMeta

    def __init__(self, buffer_length=0, **kwargs):
        """Derived.__init__ must call this at some point."""
        super(DataDevice, self).__init__(**kwargs)
        # A thread to fetch and dispatch data.
        self._fetch_thread = None
        # A flag to control the _fetch_thread.
        self._fetch_thread_run = False
        # A flag to indicate that this class uses a fetch callback.
        self._using_callback = False
        # Clients to which we send data.
        self._clientStack = []
        # A set of live clients to avoid repeated dispatch to disconnected client.
        self._liveClients = set()
        # A thread to dispatch data.
        self._dispatch_thread = None
        # A buffer for data dispatch.
        self._dispatch_buffer = queue.Queue(maxsize=buffer_length)
        # A flag to indicate if device is ready to acquire.
        self._acquiring = False
        # A condition to signal arrival of a new data and unblock grab_next_data
        self._new_data_condition = threading.Condition()

    def __del__(self):
        self.disable()
        super().__del__()

    # Wrap set_setting to pause and resume acquisition.
    set_setting = keep_acquiring(Device.set_setting)

    @abc.abstractmethod
    def abort(self):
        """Stop acquisition as soon as possible."""
        self._acquiring = False

    def enable(self):
        """Enable the data capture device.

        Ensures that a data handling threads are running.
        Implement device-specific code in _on_enable .
        """
        self._logger.debug("Enabling ...")
        if self._using_callback:
            if self._fetch_thread:
                self._fetch_thread_run = False
        else:
            if not self._fetch_thread or not self._fetch_thread.is_alive():
                self._fetch_thread = Thread(target=self._fetch_loop)
                self._fetch_thread.daemon = True
                self._fetch_thread.start()
        if not self._dispatch_thread or not self._dispatch_thread.is_alive():
            self._dispatch_thread = Thread(target=self._dispatch_loop)
            self._dispatch_thread.daemon = True
            self._dispatch_thread.start()
        # Call device-specific code.
        try:
            result = self._on_enable()
        except Exception as err:
            self._logger.debug("Error in _on_enable:", exc_info=err)
            self.enabled = False
            raise err
        if not result:
            self.enabled = False
        else:
            self.enabled = True
        self._logger.debug("... enabled.")
        return self.enabled


    def disable(self):
        """Disable the data capture device.

        Implement device-specific code in _on_disable ."""
        self.enabled = False
        if self._fetch_thread:
            if self._fetch_thread.is_alive():
                self._fetch_thread_run = False
                self._fetch_thread.join()
        super(DataDevice, self).disable()

    @abc.abstractmethod
    def _fetch_data(self):
        """Poll for data and return it, with minimal processing.

        If the device uses buffering in software, this function should copy
        the data from the buffer, release or recycle the buffer, then return
        a reference to the copy. Otherwise, if the SDK returns a data object
        that will not be written to again, this function can just return a
        reference to the object.
        If no data is available, return None.
        """
        return None

    def _process_data(self, data):
        """Do any data processing and return data."""
        return data

    def _send_data(self, client, data, timestamp):
        """Dispatch data to the client."""
        try:
            # Currently uses legacy receiveData. Would like to pass
            # this function name as an argument to set_client, but
            # not sure how to subsequently resolve this over Pyro.
            client.receiveData(data, timestamp)
        except (Pyro4.errors.ConnectionClosedError, Pyro4.errors.CommunicationError):
            # Client not listening
            self._logger.info("Removing %s from client stack: disconnected." % client._pyroUri)
            self._clientStack = list(filter(client.__ne__, self._clientStack))
            self._liveClients = self._liveClients.difference([client])
        except:
            raise

    def _dispatch_loop(self):
        """Process data and send results to any client."""
        while True:
            client, data, timestamp = self._dispatch_buffer.get(block=True)
            if client not in self._liveClients:
                continue
            err = None
            if isinstance(data, Exception):
                standard_exception = Exception(str(data).encode('ascii'))
                try:
                    self._send_data(client, standard_exception, timestamp)
                except Exception as e:
                    err = e
            else:
                try:
                    self._send_data(client, self._process_data(data), timestamp)
                except Exception as e:
                    err = e
            if err:
                # Raising an exception will kill the dispatch loop. We need another
                # way to notify the client that there was a problem.
                self._logger.error("in _dispatch_loop:", exc_info=err)
            self._dispatch_buffer.task_done()

    def _fetch_loop(self):
        """Poll source for data and put it into dispatch buffer."""
        self._fetch_thread_run = True

        while self._fetch_thread_run:
            try:
                data = self._fetch_data()
            except Exception as e:
                self._logger.error("in _fetch_loop:", exc_info=e)
                # Raising an exception will kill the fetch loop. We need another
                # way to notify the client that there was a problem.
                timestamp = time.time()
                self._put(e, timestamp)
                data = None
            if data is not None:
                # ***TODO*** Add support for timestamp from hardware.
                timestamp = time.time()
                self._put(data, timestamp)
            else:
                time.sleep(0.001)

    @property
    def _client(self):
        """A getter for the current client."""
        return (self._clientStack or [None])[-1]

    @_client.setter
    def _client(self, val):
        """Push or pop a client from the _clientStack."""
        if val is None:
            self._clientStack.pop()
        else:
            self._clientStack.append(val)
        self._liveClients = set(self._clientStack)

    def _put(self, data, timestamp):
        """Put data and timestamp into dispatch buffer with target dispatch client."""
        self._dispatch_buffer.put((self._client, data, timestamp))

    def set_client(self, new_client):
        """Set up a connection to our client.

        Clients now sit in a stack so that a single device may send
        different data to multiple clients in a single experiment.
        The usage is currently::

            device.set_client(client) # Add client to top of stack
            # do stuff, send triggers, receive data
            device.set_client(None)   # Pop top client off stack.

        There is a risk that some other client calls ``None`` before
        the current client is finished.  Avoiding this will require
        rework here to identify the caller and remove only that caller
        from the client stack.
        """
        if new_client is not None:
            if isinstance(new_client, (str, Pyro4.core.URI)):
                self._client = Pyro4.Proxy(new_client)
            else:
                self._client = new_client
        else:
            self._client = None
        # _client uses a setter. Log the result of assignment.
        if self._client is None:
            self._logger.info("Current client is None.")
        else:
            self._logger.info("Current client is %s." % str(self._client))


    @keep_acquiring
    def update_settings(self, settings, init=False):
        """Update settings, toggling acquisition if necessary."""
        super(DataDevice, self).update_settings(settings, init)

    # noinspection PyPep8Naming
    def receiveClient(self, client_uri):
        """A passthrough for compatibility."""
        self.set_client(client_uri)

    def grab_next_data(self, soft_trigger=True):
        """Returns results from next trigger via a direct call.

        :param soft_trigger: calls soft_trigger if True,
                               waits for hardware trigger if False.
        """
        self._new_data_condition.acquire()
        # Push self onto client stack.
        self.set_client(self)
        # Wait for data from next trigger.
        if soft_trigger:
            self.soft_trigger()
        self._new_data_condition.wait()
        # Pop self from client stack
        self.set_client(None)
        # Return the data.
        return self._new_data

    # noinspection PyPep8Naming
    def receiveData(self, data, timestamp):
        """Unblocks grab_next_frame so it can return."""
        with self._new_data_condition:
            self._new_data = (data, timestamp)
            self._new_data_condition.notify()


class CameraDevice(DataDevice):
    """Adds functionality to DataDevice to support cameras.

    Defines the interface for cameras.
    Applies a transform to acquired data in the processing step.
    """
    ALLOWED_TRANSFORMS = [p for p in itertools.product(*3 * [[False, True]])]

    def __init__(self, **kwargs):
        super(CameraDevice, self).__init__(**kwargs)
        # A list of readout mode descriptions.
        self._readout_modes = ['default']
        # The index of the current readout mode.
        self._readout_mode = 0
        # Transforms to apply to data (fliplr, flipud, rot90)
        # Transform to correct for readout order.
        self._readout_transform = (False, False, False)
        # Transform supplied by client to correct for system geometry.
        self._client_transform = (False, False, False)
        # Result of combining client and readout transforms
        self._transform = (False, False, False)
        # A transform provided by the client.
        self.add_setting('transform', 'enum',
                         lambda: CameraDevice.ALLOWED_TRANSFORMS.index(self._transform),
                         lambda index: self.set_transform(CameraDevice.ALLOWED_TRANSFORMS[index]),
                         CameraDevice.ALLOWED_TRANSFORMS)
        self.add_setting('readout mode', 'enum',
                         lambda: self._readout_mode,
                         self.set_readout_mode,
                         lambda: self._readout_modes)
        self.add_setting('binning', 'tuple',
                         self.get_binning,
                         self.set_binning,
                         None)
        self.add_setting('roi', 'tuple',
                         self.get_roi,
                         self.set_roi,
                         None)
    def _process_data(self, data):
        """Apply self._transform to data."""
        flips = (self._transform[0], self._transform[1])
        rot = self._transform[2]

        # Choose appropriate transform based on (flips, rot).
        return {(0, 0): numpy.rot90(data, rot),
                (0, 1): numpy.flipud(numpy.rot90(data, rot)),
                (1, 0): numpy.fliplr(numpy.rot90(data, rot)),
                (1, 1): numpy.fliplr(numpy.flipud(numpy.rot90(data, rot)))
                }[flips]


    def set_readout_mode(self, description):
        """Set the readout mode and _readout_transform."""
        pass

    def get_transform(self):
        """Return the current transform without readout transform."""
        return self._client_transform

    def set_transform(self, transform):
        """Combine provided transform with readout transform."""
        if isinstance(transform, str):
            transform = literal_eval(transform)
        self._client_transform = transform
        lr, ud, rot = (self._readout_transform[i] ^ transform[i] for i in range(3))
        if self._readout_transform[2] and self._client_transform[2]:
            lr = not lr
            ud = not ud
        self._transform = (lr, ud, rot)

    def _set_readout_transform(self, new_transform):
        """Update readout transform and update resultant transform."""
        self._readout_transform = [bool(int(t)) for t in new_transform]
        self.set_transform(self._client_transform)

    @abc.abstractmethod
    def set_exposure_time(self, value):
        """Set the exposure time on the device.

        :param value: exposure time in seconds
        """
        pass

    def get_exposure_time(self):
        """Return the current exposure time, in seconds."""
        pass

    def get_cycle_time(self):
        """Return the cycle time, in seconds."""
        pass

    def get_sensor_temperature(self):
        """Return the sensor temperature."""
        pass

    @abc.abstractmethod
    def _get_sensor_shape(self):
        """Return a tuple of (width, height) indicating shape in pixels."""
        pass

    def get_sensor_shape(self):
        """Return a tuple of (width, height), corrected for transform."""
        shape = self._get_sensor_shape()
        if self._transform[2]:
            # 90 degree rotation
            shape = (shape[1], shape[0])
        return shape

    @abc.abstractmethod
    def _get_binning(self):
        """Return a tuple of (horizontal, vertical)"""
        pass

    def get_binning(self):
        """Return a tuple of (horizontal, vertical), corrected for transform."""
        binning = self._get_binning()
        if self._transform[2]:
            # 90 degree rotation
            binning = (binning[1], binning[0])
        return binning

    @abc.abstractmethod
    def _set_binning(self, binning):
        """Set binning along both axes. Return True if successful."""
        pass

    def set_binning(self, binning):
        """Set binning along both axes. Return True if successful."""
        h_bin, v_bin = binning
        if self._transform[2]:
            # 90 degree rotation
            binning = Binning(v_bin, h_bin)
        else:
            binning = Binning(h_bin, v_bin)
        return self._set_binning(binning)

    @abc.abstractmethod
    def _get_roi(self):
        """Return the ROI as it is on hardware."""
        return ROI(left, top, width, height)

    def get_roi(self):
        """Return ROI as a rectangle (left, top, width, height).

        Chosen this rectangle format as it completely defines the ROI without
        reference to the sensor geometry."""
        roi = self._get_roi()
        if self._transform[2]:
            # 90 degree rotation
            roi = ROI(roi[1], roi[0], roi[3], roi[2])
        return roi

    @abc.abstractmethod
    def _set_roi(self, roi):
        """Set the ROI on the hardware, return True if successful."""
        return False

    def set_roi(self, roi):
        """Set the ROI according to the provided rectangle.
        ROI is a tuple (left, right, width, height)
        Return True if ROI set correctly, False otherwise."""
        maxw, maxh = self.get_sensor_shape()
        binning = self.get_binning()
        left, top, width, height = roi
        if not width: # 0 or None
            width = maxw // binning.h
        if not height: # 0 o rNone
            height = maxh // binning.v
        if self._transform[2]:
            roi = ROI(left, top, height, width)
        else:
            roi = ROI(left, top, width, height)
        return self._set_roi(roi)

    def get_trigger_type(self):
        """Return the current trigger mode.

        One of
            TRIGGER_AFTER,
            TRIGGER_BEFORE or
            TRIGGER_DURATION (bulb exposure.)
        """
        pass

    def get_meta_data(self):
        """Return metadata."""
        pass

    def soft_trigger(self):
        """Optional software trigger - implement if available."""
        pass


class TriggerType(Enum):
    SOFTWARE = 0
    RISING_EDGE = 1
    FALLING_EDGE = 2
    PULSE = 3

class TriggerMode(Enum):
    ONCE = 1
    BULB = 2
    STROBE = 3
    START = 4


class TriggerTargetMixIn(object):
    """MixIn for Device that may be the target of a hardware trigger.

    Subclasses must set a `_trigger_type` and `_trigger_mode` property
    with the current trigger during object construction.

    TODO: need some way to retrieve the supported trigger types and
        modes.  We could require subclasses to define `_trigger_types`
        and `_trigger_modes` listing what is supported but would still
        not be enough since often not all trigger type and mode are
        supported.

    """
    __metaclass__ = abc.ABCMeta

    @property
    def trigger_mode(self):
        return self._trigger_mode
    @property
    def trigger_type(self):
        return self._trigger_type

    @abc.abstractmethod
    def set_trigger(self, ttype, tmode):
        """Set device for a specific trigger.
        """
        pass


class SerialDeviceMixIn(object):
    """MixIn for devices that are controlled via serial.

    Currently handles the flushing and locking of the comms channel
    until a command has finished, and the passthrough to the serial
    channel.

    TODO: add more logic to handle the code duplication of serial
    devices.
    """
    __metaclass__ = abc.ABCMeta

    def __init__(self, **kwargs):
        super(SerialDeviceMixIn, self).__init__(**kwargs)
        ## TODO: We should probably construct the connection here but
        ##       the Serial constructor takes a lot of arguments, and
        ##       it becomes tricky to separate those from arguments to
        ##       the constructor of other parent classes.
        self.connection = None # serial.Serial (to be constructed by child)
        self._comms_lock = threading.RLock()

    def _readline(self):
        """Read a line from connection without leading and trailing whitespace.
        """
        return self.connection.readline().strip()

    def _write(self, command):
        """Send a command to the device.

        This is not a simple passthrough to ``serial.Serial.write``,
        it will append ``b'\\r\\n'`` to command.  Override this method
        if a device requires a specific format.
        """
        return self.connection.write(command + b'\r\n')

    @abc.abstractmethod
    def is_alive(self):
        """Query if device is alive and we can send messages."""
        pass

    @staticmethod
    def lock_comms(func):
        """Decorator to flush input buffer and lock communications.

        There have been problems with the DeepStar lasers returning
        junk characters after the expected response, so it is
        advisable to flush the input buffer prior to running a command
        and subsequent readline.  It also locks the comms channel so
        that a function must finish all its communications before
        another can run.
        """
        @functools.wraps(func)
        def wrapper(self, *args, **kwargs):
            with self._comms_lock:
                self.connection.flushInput()
                return func(self, *args, **kwargs)
        return wrapper


class DeformableMirror(Device):
    """Base class for Deformable Mirrors.

    There is no method to reset or clear a deformable mirror.  While
    different vendors provide functions to do that, it is unclear
    exactly what it does the actuators.  Does it set all actuators
    back to something based on a calibration file?  Does it apply a
    voltage of zero to each?  Does it set the values to zero and what
    does that mean since different deformable mirrors expect values in
    a different range?  For the sake of uniformity, it is better for
    python-microscope users to pass the pattern they want, probably a
    pattern that flattens the mirror.

    It is also unclear what the use case for a reset.  If it just to
    set the mirror to an initial state and not a specific shape, then
    destroying and re-constructing the DeformableMirror object
    provides the most obvious solution.
    """
    __metaclass__ = abc.ABCMeta

    @abc.abstractmethod
    def __init__(self, **kwargs):
        """Constructor.

        Subclasses must define the following properties during
        construction:

            _n_actuators : int

        In addition, the private properties `_patterns` and
        `_pattern_idx` are initialized to None to support the queueing
        of patterns and software triggering.
        """
        super(DeformableMirror, self).__init__(**kwargs)

        self._patterns = None
        self._patterns_idx = None

    @property
    def n_actuators(self):
        return self._n_actuators

    def _validate_patterns(self, patterns):
        """Validate the shape of a series of patterns.

        Only validates the shape of the patterns, not if the values
        are actually in the [0 1] range.  If some hardware is unable
        to handle values outside their defined range (most will simply
        clip them), then it's the responsability of the subclass to do
        the clipping before sending the values.
        """
        if patterns.ndim > 2:
            raise Exception("PATTERNS has %d dimensions (must be 1 or 2)"
                            % patterns.ndim)
        elif patterns.shape[-1] != self._n_actuators:
            raise Exception(("PATTERNS length of second dimension '%d' differs"
                             " differs from number of actuators '%d'"
                             % (patterns.shape[-1], self._n_actuators)))

    @abc.abstractmethod
    def apply_pattern(self, pattern):
        """Apply this pattern.
        """
        pass

    def queue_patterns(self, patterns):
        """Send values to the mirror.

        Parameters
        ----------
        patterns : numpy.array
            An KxN elements array of values in the range [0 1], where N
            equals the number of actuators, and K is the number of
            patterns.

        A convenience fallback is provided for software triggering is
        provided.
        """
        self._validate_patterns(patterns)
        self._patterns = patterns
        self._pattern_idx = -1 # none is applied yet

    def next_pattern(self):
        """Apply the next pattern in the queue.

        A convenience fallback is provided.
        """
        if self._patterns is None:
            raise Exception("no pattern queued to apply")
        self._pattern_idx += 1
        self.apply_pattern(self._patterns[self._pattern_idx,:])

    def initialize(self):
        pass

    def _on_shutdown(self):
        pass


class LaserDevice(Device):
    __metaclass__ = abc.ABCMeta

    @abc.abstractmethod
    def __init__(self, **kwargs):
        super(LaserDevice, self).__init__(**kwargs)
        self._set_point = None

    @abc.abstractmethod
    def get_status(self):
        """Query and return the laser status."""
        result = []
        # ...
        return result

    @abc.abstractmethod
    def get_is_on(self):
        """Return True if the laser is currently able to produce light."""
        pass

    @abc.abstractmethod
    def get_min_power_mw(self):
        """Return the min power in mW."""
        pass

    @abc.abstractmethod
    def get_max_power_mw(self):
        """Return the max. power in mW."""
        pass

    @abc.abstractmethod
    def get_power_mw(self):
        """"" Return the current power in mW."""
        pass

    def get_set_power_mw(self):
        """Return the power set point."""
        return self._set_point

    @abc.abstractmethod
    def _set_power_mw(self, mw):
        """Set the power on the device in mW."""
        pass

    def set_power_mw(self, mw):
        """Set the power from an argument in mW and save the set point.

        Args:
            mw (float): Power in mW.  Value will be clipped to the
                valid range for the laser.  See the methods
                :func:`get_max_power_mw` and :func:`get_min_power_mw`
                to retrieve the valid range.

        Returns:
            void
        """
        mw = max(min(mw, self.get_max_power_mw()), self.get_min_power_mw())
        self._set_point = mw
        self._set_power_mw(mw)


class FilterWheelBase(Device):
    __metaclass__ = abc.ABCMeta

    def __init__(self, filters=[], positions=0, **kwargs):
        super(FilterWheelBase, self).__init__(**kwargs)
        if isinstance(filters, dict):
            self._filters = filters
        else:
            self._filters = {i:f for (i, f) in enumerate(filters)}
        self._inv_filters = {val: key for key, val in self._filters.items()}
        if not hasattr(self, '_positions'):
            self._positions = positions
        # The position as an integer.
        # Deprecated: clients should call get_position and set_position;
        # still exposed as a setting until cockpit uses set_position.
        self.add_setting('position',
                         'int',
                         self.get_position,
                         self.set_position,
                         lambda: (0, self.get_num_positions()) )


    def get_num_positions(self):
        """Returns the number of wheel positions."""
        return(max( self._positions, len(self._filters)))

    @abc.abstractmethod
    def get_position(self):
        """Return the wheel's current position"""
        return 0

    @abc.abstractmethod
    def set_position(self, position):
        """Set the wheel position."""
        pass

    def get_filters(self):
        return [(k,v) for k,v in self._filters.items()]
