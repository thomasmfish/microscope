#!/usr/bin/env python
# -*- coding: utf-8 -*-

## Copyright (C) 2017 David Pinto <david.pinto@bioch.ox.ac.uk>
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

import ctypes
import warnings

import Pyro4
import numpy

from microscope.devices import DeformableMirror
from microscope.devices import TriggerMode
from microscope.devices import TriggerTargetMixIn
from microscope.devices import TriggerType

import microscope._wrappers.asdk as asdk


class AlpaoDeformableMirror(TriggerTargetMixIn, DeformableMirror):
    """Class for Alpao deformable mirror.

    The Alpao mirrors have support for hardware triggering.  By default,
    it will be configured for software triggering, and trigger once.
    """
    ## The length of the buffer given to Alpao SDK to write error
    ## messages.
    _err_msg_len = 64

    _TriggerType_to_asdkTriggerIn = {
        TriggerType.SOFTWARE : 0,
        TriggerType.RISING_EDGE : 1,
        TriggerType.FALLING_EDGE : 2,
    }

    _supported_TriggerModes = [
        TriggerMode.ONCE,
        TriggerMode.START,
    ]


    @staticmethod
    def _normalize_patterns(patterns):
        """
        Alpao SDK expects values in the [-1 1] range, so we normalize
        them from the [0 1] range we expect in our interface.
        """
        patterns = (patterns * 2) -1
        return patterns

    def _find_error_str(self):
        """Get an error string from the Alpao SDK error stack.

        Returns
        -------
        A string.  Will be empty if there was no error on the stack.
        """
        ## asdkGetLastError should write a null-terminated string but
        ## doesn't seem like it (at least CannotOpenCfg does not ends in
        ## null) so we empty the buffer ourselves before using it.  Note
        ## that even when there are no errors, we need to empty the buffer
        ## because the buffer has the message 'No error in stack'.
        ##
        ## TODO: report this upstream to Alpao and clean our code.
        self._err_msg[0:self._err_msg_len] = b'\x00' * self._err_msg_len

        err = ctypes.pointer(asdk.UInt(0))
        status = asdk.GetLastError(err, self._err_msg, self._err_msg_len)
        if status == asdk.SUCCESS:
            msg = self._err_msg.value
            if len(msg) > self._err_msg_len:
                msg = msg + "..."
            msg += "(error %i)" % (err.contents.value)
            return msg
        else:
            return ""

    def _raise_if_error(self, status, exception_cls=Exception):
        if status != asdk.SUCCESS:
            msg = self._find_error_str()
            if msg:
                raise exception_cls(msg)


    def __init__(self, serial_number, **kwargs):
        """
        Parameters
        ----------
        serial_number: string
        The serial number of the deformable mirror, something like "BIL103".
        """
        super(AlpaoDeformableMirror, self).__init__(**kwargs)

        ## We need to constantly check for errors and need a buffer to
        ## have the message written to.  To avoid creating a new buffer
        ## each time, have a buffer per instance.
        self._err_msg = ctypes.create_string_buffer(self._err_msg_len)

        self._dm = asdk.Init(serial_number.encode())
        if not self._dm:
            raise Exception("Failed to initialise connection: don't know why")
        ## In theory, asdkInit should return a NULL pointer in case of
        ## failure and that should be enough to check.  However, at least
        ## in the case of a missing configuration file it still returns a
        ## DM pointer so we still need to check for errors on the stack.
        self._raise_if_error(asdk.FAILURE)

        value = asdk.Scalar_p(asdk.Scalar())
        status = asdk.Get(self._dm, b'NbOfActuator', value)
        self._raise_if_error(status)
        self._n_actuators = int(value.contents.value)
        self._trigger_type = TriggerType.SOFTWARE
        self._trigger_mode = TriggerMode.ONCE

    def apply_pattern(self, pattern):
        self._validate_patterns(pattern)
        pattern = self._normalize_patterns(pattern)
        data_pointer = pattern.ctypes.data_as(asdk.Scalar_p)
        status = asdk.Send(self._dm, data_pointer)
        self._raise_if_error(status)

    def set_trigger(self, ttype, tmode):
        if tmode not in self._supported_TriggerModes:
            raise Exception("unsupported trigger of mode '%s' for Alpao Mirrors"
                            % tmode.name)
        elif ttype == TriggerType.SOFTWARE and tmode != TriggerMode.ONCE:
            raise Exception("trigger mode '%s' only supports trigger type ONCE"
                            % tmode.name)
        self._trigger_mode = tmode

        try:
            value = self._TriggerType_to_asdkTriggerIn[ttype]
        except KeyError:
            raise Exception("unsupported trigger of type '%s' for Alpao Mirrors"
                            % ttype.name)
        status = asdk.Set(self._dm, b'TriggerIn', value)
        self._raise_if_error(status)
        self._trigger_type = ttype

    def queue_patterns(self, patterns):
        if self._trigger_type == TriggerType.SOFTWARE:
            super(AlpaoDeformableMirror, self).queue_patterns(patterns)
            return

        self._validate_patterns(patterns)
        patterns = self._normalize_patterns(patterns)
        patterns = numpy.atleast_2d(patterns)
        n_patterns = patterns.shape[0]

        ## The Alpao SDK seems to only support the trigger mode start.  It
        ## still has option called nRepeats that we can't really figure
        ## what is meant to do.  When set to 1, the mode is start.  What
        ## we want it is to have trigger mode once which was not
        ## supported.  We have received a modified version where if
        ## nRepeats is set to same number of patterns, does trigger mode
        ## once (not documented on Alpao SDK).
        if self._trigger_mode == TriggerMode.ONCE:
            n_repeats = n_patterns
        elif self._trigger_mode == TriggerMode.START:
            n_repeats = 1
        else:
            raise Exception("trigger type '%s' and trigger mode '%s'"
                            % (self._trigger_type.name,
                               self._trigger_mode.name))

        data_pointer = patterns.ctypes.data_as(asdk.Scalar_p)
        status = asdk.SendPattern(self._dm, data_pointer, n_patterns, n_repeats)
        self._raise_if_error(status)

    def next_pattern(self):
        if self.trigger_type == TriggerType.SOFTWARE:
            super(AlpaoDeformableMirror, self).next_pattern()
        else:
            raise Exception("software trigger received when set for"
                            " hardware trigger")

    def __del__(self):
        status = asdk.Release(self._dm)
        if status != asdk.SUCCESS:
            msg = self._find_error_str()
            warnings.warn(msg)
        super(AlpaoDeformableMirror, self).__del__()
