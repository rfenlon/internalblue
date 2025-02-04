#!/usr/bin/env python2

from __future__ import absolute_import

import datetime
import fcntl
import queue as queue2k
import socket
import struct
import threading
from builtins import range
from builtins import str
from builtins import zip

from ctypes import *

from typing import List, cast, TYPE_CHECKING

from future import standard_library

from . import hci
from .core import InternalBlue
from .utils.packing import p16, u16, p32, u32

if TYPE_CHECKING:
    from internalblue import Device

standard_library.install_aliases()


class sockaddr_hci(Structure):
    _fields_ = [
        ("sin_family",      c_ushort),
        ("hci_dev",         c_ushort),
        ("hci_channel",     c_ushort),
    ]

# from /usr/include/bluetooth/hci.h:
# define HCIDEVUP	_IOW('H', 201, int)
# define HCIGETDEVLIST	_IOR('H', 210, int)
# define HCIGETDEVINFO	_IOR('H', 211, int)

# ioctl numbers. see http://code.activestate.com/recipes/578225-linux-ioctl-numbers-in-python/
def _IOR(_type, nr, size):
    return 2 << 30 | _type << 8 | nr << 0 | size << 16


def _IOW(_type, nr, size):
    return 1 << 30 | _type << 8 | nr << 0 | size << 16


HCIDEVUP = _IOW(ord("H"), 201, 4)
HCIGETDEVLIST = _IOR(ord("H"), 210, 4)
HCIGETDEVINFO = _IOR(ord("H"), 211, 4)


class HCICore(InternalBlue):
    def __init__(
            self,
            queue_size=1000,
            btsnooplog_filename="btsnoop.log",
            log_level="info",
            data_directory=".",
            replay=False,
            user_channel=False,
    ):
        super(HCICore, self).__init__(
            queue_size,
            btsnooplog_filename,
            log_level,
            data_directory,
            replay,
        )
        self.btsnooplog_file_lock = threading.Lock()
        self.serial = False
        self.doublecheck = False
        self.user_channel = user_channel

    def getHciDeviceList(self):
        # type: () -> List[Device]
        """
        Get a list of available HCI devices. The list is obtained by executing
        ioctl syscalls HCIGETDEVLIST and HCIGETDEVINFO. The returned list 
        contains dictionaries with the following fields:
            dev_id          : Internal ID of the device (e.g. 0)
            dev_name        : Name of the device (e.g. "hci0")
            dev_bdaddr      : MAC address (e.g. "00:11:22:33:44:55")
            dev_flags       : Device flags as decimal number
            dev_flags_str   : Device flags as String (e.g. "UP RUNNING" or "DOWN")
        """

        # Open Bluetooth socket to execute ioctl's:
        try:
            s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_RAW, socket.BTPROTO_HCI)
        # Ticket 6: does not run on Windows with Kali subsystem
        except socket.error:
            self.logger.warn(
                "Opening a local Bluetooth socket failed. Not running on native Linux?"
            )
            return []

        # Do ioctl(s,HCIGETDEVLIST,arg) to get the number of available devices:
        # arg is struct hci_dev_list_req (/usr/include/bluetooth/hci.h)
        arg = p32(16)  # dl->dev_num = HCI_MAX_DEV which is 16 (little endian)
        arg += b"\x00" * (8 * 16)
        devices_raw = fcntl.ioctl(s.fileno(), HCIGETDEVLIST, arg)
        num_devices = u16(devices_raw[:2])
        self.logger.debug("Found %d HCI devices via ioctl(HCIGETDEVLIST)!" % num_devices)

        device_list = []
        for dev_nr in range(num_devices):
            dev_struct_start = 4 + 8 * dev_nr
            dev_id = u16(devices_raw[dev_struct_start: dev_struct_start + 2])
            # arg is struct hci_dev_info (/usr/include/bluetooth/hci.h)
            arg = p16(dev_id)  # di->dev_id = <device_id>
            arg += b"\x00" * 20  # Enough space for name, bdaddr and flags
            dev_info_raw = bytearray(fcntl.ioctl(s.fileno(), HCIGETDEVINFO, arg))
            dev_name = dev_info_raw[2:10].replace(b"\x00", b"").decode()
            dev_bdaddr = ":".join(["%02X" % x for x in dev_info_raw[10:16][::-1]])
            dev_flags = u32(dev_info_raw[16:20])
            if dev_flags == 0:
                dev_flags_str = "DOWN"
            else:
                dev_flags_str = " ".join(
                    [
                        name
                        for flag, name in zip(
                            bin(dev_flags)[2:][::-1],
                            [
                                "UP",
                                "INIT",
                                "RUNNING",
                                "PSCAN",
                                "ISCAN",
                                "AUTH",
                                "ENCRYPT",
                                "INQUIRY",
                                "RAW",
                                "RESET",
                            ],
                        )
                        if flag == "1"
                    ]
                )

            device_list.append(
                {
                    "dev_id": dev_id,
                    "dev_name": dev_name,
                    "dev_bdaddr": dev_bdaddr,
                    "dev_flags": dev_flags,
                    "dev_flags_str": dev_flags_str,
                }
            )
        s.close()
        return cast("List[Device]", device_list)

    def bringHciDeviceUp(self, dev_id):
        """
        Uses HCIDEVUP ioctl to bring HCI device with id dev_id up.
        Requires root priviledges (CAP_NET_ADMIN).
        """

        if dev_id < 0 or dev_id > 16:
            self.logger.warn("bringHciDeviceUp: Invalid device id: %d." % dev_id)
            return False

        # Open bluetooth socket to execute ioctl's:
        s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_RAW, socket.BTPROTO_HCI)

        # Do ioctl(s, HCIDEVUP, dev_id) to bring device up:
        try:
            fcntl.ioctl(s.fileno(), HCIDEVUP, dev_id)
            s.close()
            self.logger.info("Device with id=%d was set up successfully!" % dev_id)
            return True
        except IOError as e:
            s.close()
            self.logger.warn("Error returned by ioctl: %s" % str(e))
            return False

    def device_list(self):
        """
        Return a list of connected hci devices.
        """
        if self.replay:
            return [(self, "hci_replay", "hci: ReplaySocket")]
        device_list = []
        for dev in self.getHciDeviceList():
            self.logger.info(
                "HCI device: %s  [%s]  flags=%d<%s>"
                % (
                    dev["dev_name"],
                    dev["dev_bdaddr"],
                    dev["dev_flags"],
                    dev["dev_flags_str"],
                )
            )
            device_list.append(
                (
                    self,
                    dev["dev_name"],
                    "hci: %s (%s) <%s>"
                    % (dev["dev_bdaddr"], dev["dev_name"], dev["dev_flags_str"]),
                )
            )

        if len(device_list) == 0:
            self.logger.info("No connected HCI device found")

        return cast("List[Device]", device_list)

    def local_connect(self):
        """
        """

        if not self.interface:
            self.logger.warn("No HCI identifier is set")
            return False

        if self.user_channel:
            success = self._setupSocketsUserChannel()
        else:
            success = self._setupSockets()

        if not success:
            self.logger.critical("HCI socket could not be established!")
            return False

        return True

    def _btsnoop_pack_time(self, time):
        """
        Takes datetime object and returns microseconds since 2000-01-01.

        see https://github.com/joekickass/python-btsnoop

        Record time is a 64-bit signed integer representing the time of packet arrival,
        in microseconds since midnight, January 1st, 0 AD nominal Gregorian.

        In order to avoid leap-day ambiguity in calculations, note that an equivalent
        epoch may be used of midnight, January 1st 2000 AD, which is represented in
        this field as 0x00E03AB44A676000.
        """
        time_betw_0_and_2000_ad = int("0x00E03AB44A676000", 16)
        time_since_2000_epoch = time - datetime.datetime(2000, 1, 1)
        packed_time = time_since_2000_epoch + datetime.timedelta(
            microseconds=time_betw_0_and_2000_ad
        )
        return int(packed_time.total_seconds() * 1000 * 1000)

    def _recvThreadFunc(self):
        """
        This is the run-function of the recvThread. It receives HCI events from the
        s_snoop socket. The HCI packets are encapsulated in btsnoop records (see RFC 1761).
        Received HCI packets are being put into the queues inside registeredHciRecvQueues and
        passed to the callback functions inside registeredHciCallbacks.
        The thread stops when exit_requested is set to True. It will do that on its own
        if it encounters a fatal error or the stackDumpReceiver reports that the chip crashed.
        """

        self.logger.debug("Receive Thread started.")

        while not self.exit_requested:
            # Read the record data
            try:
                record_data = self.s_snoop.recv(1024)
                record_data = bytearray(record_data)
            except socket.timeout:
                continue  # this is ok. just try again without error
            except Exception as e:
                self.logger.critical(
                    "Lost device interface with exception {}, terminating receive thread...".format(
                        e
                    )
                )
                self.exit_requested = True
                continue

            # btsnoop record header data:
            btsnoop_orig_len = len(record_data)
            btsnoop_inc_len = len(record_data)
            btsnoop_flags = 0
            btsnoop_drops = 0
            btsnoop_time = datetime.datetime.now()

            if btsnoop_orig_len == 0:
                continue

            # Put all relevant infos into a tuple. The HCI packet is parsed with the help of hci.py.
            record = (
                hci.parse_hci_packet(record_data),
                btsnoop_orig_len,
                btsnoop_inc_len,
                btsnoop_flags,
                btsnoop_drops,
                btsnoop_time,
            )

            self.logger.debug(
                "_recvThreadFunc Recv: [" + str(btsnoop_time) + "] " + str(record[0])
            )

            # Write to btsnoop file:
            if self.write_btsnooplog:
                btsnoop_record_hdr = struct.pack(
                    ">IIIIq",
                    btsnoop_orig_len,
                    btsnoop_inc_len,
                    btsnoop_flags,
                    btsnoop_drops,
                    self._btsnoop_pack_time(btsnoop_time),
                )
                with self.btsnooplog_file_lock:
                    self.btsnooplog_file.write(btsnoop_record_hdr)
                    self.btsnooplog_file.write(record_data)
                    self.btsnooplog_file.flush()

            # Put the record into all queues of registeredHciRecvQueues if their
            # filter function matches.
            for queue, filter_function in self.registeredHciRecvQueues:
                if filter_function is None or filter_function(record):
                    try:
                        queue.put(record, block=False)
                    except queue2k.Full:
                        self.logger.warn(
                            "recvThreadFunc: A recv queue is full. dropping packets.."
                        )

            # Call all callback functions inside registeredHciCallbacks and pass the
            # record as argument.
            for callback in self.registeredHciCallbacks:
                callback(record)

            # Check if the stackDumpReceiver has noticed that the chip crashed.
            # if self.stackDumpReceiver.stack_dump_has_happened:
            # A stack dump has happened!
            # self.logger.warn("recvThreadFunc: The controller send a stack dump.")
            # self.exit_requested = True

        self.logger.debug("Receive Thread terminated.")

    def _writeBTSnoopHeader(self):
        # Write Header to btsnoop file (if file is still empty):
        if self.write_btsnooplog and self.btsnooplog_file.tell() == 0:
            # BT Snoop Header: btsnoop\x00, version: 1, data link type: 1002
            btsnoop_hdr = (
                    b"btsnoop\x00" + p32(1, endian="big") + p32(1002, endian="big")
            )
            with self.btsnooplog_file_lock:
                self.btsnooplog_file.write(btsnoop_hdr)
                self.btsnooplog_file.flush()


    def _setupSockets(self):
        """
        Linux already allows to open HCI sockets to Bluetooth devices,
        they include H4 information, we simply use it.
        """

        # Check if hci device is in state "UP". If not, set it to "UP" (requires root)
        device = [
            dev for dev in self.getHciDeviceList() if dev["dev_name"] == self.interface
        ]
        if len(device) == 0:
            self.logger.warn("Device not found: " + self.interface)
            return False
        device = device[0]

        if device["dev_flags"] == 0:
            self.logger.warn("Device %s is DOWN!" % self.interface)
            self.logger.info("Trying to set %s to state 'UP' (requires root)" % self.interface)
            if not self.bringHciDeviceUp(device["dev_id"]):
                self.logger.warn("Failed to bring up %s." % self.interface)
                return False

        # TODO unload btusb module and check error messages here to give the user some output if sth fails

        # Connect to HCI socket
        self.s_snoop = socket.socket(
            socket.AF_BLUETOOTH, socket.SOCK_RAW, socket.BTPROTO_HCI
        )
        self.s_snoop.setsockopt(socket.SOL_HCI, socket.HCI_DATA_DIR, 1)
        self.s_snoop.setsockopt(socket.SOL_HCI, socket.HCI_TIME_STAMP, 1)
        """
        struct hci_filter {
            uint32_t type_mask;     -> 4
            uint32_t event_mask[2]; -> 8
            uint16_t opcode;        -> 2
        };
        """
        # TODO still seems to only forward incoming events?!
        self.s_snoop.setsockopt(
            socket.SOL_HCI,
            socket.HCI_FILTER,
            b"\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\x00\x00\x00\x00",
        )  # type mask, event mask, event mask, opcode

        interface_num = device["dev_id"]
        self.logger.debug("Socket interface number: %s" % interface_num)
        self.s_snoop.bind((interface_num,))
        self.s_snoop.settimeout(2)
        self.logger.debug("_setupSockets: Bound socket.")

        # same socket for input and output (this is different from adb here!)
        self.s_inject = self.s_snoop

        self._writeBTSnoopHeader()

        return True

    def _setupSocketsUserChannel(self):
        """
            Python's socket API does not allow to set up an HCI User Channel
            so we need to use ctypes here. Most parts of this are taken from
            scapy's code (https://github.com/secdev/scapy/blob/master/scapy/layers/bluetooth.py#L1482)
        """

        sockaddr_hcip = POINTER(sockaddr_hci)
        cdll.LoadLibrary("libc.so.6")
        libc = CDLL("libc.so.6")

        socket_c = libc.socket
        socket_c.argtypes = (c_int, c_int, c_int);
        socket_c.restype = c_int

        bind = libc.bind
        bind.argtypes = (c_int, POINTER(sockaddr_hci), c_int)
        bind.restype = c_int

        s = socket_c(31, 3, 1) # (AF_BLUETOOTH, SOCK_RAW, HCI_CHANNEL_USER)
        if s < 0:
            self.logger.error("Unable to open PF_BLUETOOTH socket")

        sa = sockaddr_hci()
        sa.sin_family = 31  # AF_BLUETOOTH
        sa.hci_dev = 0      # adapter index
        sa.hci_channel = 1  # HCI_USER_CHANNEL

        r = bind(s, sockaddr_hcip(sa), sizeof(sa))
        if r != 0:
            self.logger.error("Unable to bind")

        self.s_snoop = socket.fromfd(s, 31, 3, 1)

        # same socket for input and output (this is different from adb here!)
        self.s_inject = self.s_snoop

        self._writeBTSnoopHeader()

        return True

    def _teardownSockets(self):
        """
        Close s_snoop and s_inject socket. (equal)
        """

        if self.s_inject is not None:
            self.s_inject.close()
            self.s_inject = None
            self.s_snoop = None

        return True
