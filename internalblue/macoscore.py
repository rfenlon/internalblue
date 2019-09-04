#!/usr/bin/env python2

import socket
import Queue
import hci

from pwn import *

from core import InternalBlue
from abc import ABCMeta, abstractmethod

import sys
import ctypes
import objc
from Foundation import *
from ctypes import CDLL, c_void_p, byref, c_char_p
from ctypes.util import find_library
from Foundation import NSMutableArray

import threading
import binascii

objc.initFrameworkWrapper("IOBluetoothExtended",
	frameworkIdentifier="com.davidetoldo.IOBluetoothExtended",
	frameworkPath=objc.pathForFramework("../macos-framework/IOBluetoothExtended.framework"),
	globals=globals())

class macOSCore(InternalBlue):
    NSNotificationCenter = objc.lookUpClass('NSNotificationCenter')

    def __init__(self, queue_size=1000, btsnooplog_filename='btsnoop.log', log_level='info', fix_binutils='True', data_directory="."):
        super(macOSCore, self).__init__(queue_size, btsnooplog_filename, log_level, fix_binutils, data_directory=".")
        self.controller = None
        self.delegate = None

    def receivedNotification_(self, note):
        log.warn(note.userInfo()["formatted"])
        # self.s_inject = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # if self.s_inject.getsockname()[1] == 0:
        #     self.s_inject.connect(('127.0.0.1', self.hciport))
        # self.s_inject.sendall(note.userInfo()["message"].decode("hex"))

    def device_list(self):
        """
        Get a list of connected devices
        """

        if self.exit_requested:
            self.shutdown()

        if self.running:
            log.warn("Already running. Call shutdown() first!")
            return []

        # assume that a explicitly specified iPhone exists
        device_list = []
        device_list.append((self, "mac", "mac"))

        return device_list

    def sendH4(self, h4type, data, timeout=2):
        """
        Send an arbitrary HCI packet by pushing a send-task into the
        sendQueue. This function blocks until the response is received
        or the timeout expires. The return value is the Payload of the
        HCI Command Complete Event which was received in response to
        the command or None if no response was received within the timeout.
        """

        queue = Queue.Queue(1)

        try:
            self.sendQueue.put((h4type, data, queue, None), timeout=timeout)
            ret = queue.get(timeout=timeout)
            return ret
        except Queue.Empty:
            log.warn("sendH4: waiting for response timed out!")
            return None
        except Queue.Full:
            log.warn("sendH4: send queue is full!")
            return None

    def local_connect(self):
    	if not self._setupSockets():
            log.critical("No connection to target device.")
        return True

    def _setupSockets(self):
        self.hciport = 65432#random.randint(60000, 65535)
        log.debug("_setupSockets: Selected random ports snoop=%d and inject=%d" % (self.hciport, self.hciport + 1))

        self.s_snoop = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.s_snoop.bind(('127.0.0.1', self.hciport))
        self.s_snoop.settimeout(0.5)
        self.s_snoop.listen(1)

        self.s_inject = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

        self.controller = IOBluetoothHostController.defaultController()
        self.delegate = HCIDelegate.alloc().init()
        self.delegate.setWaitingFor_(0xfc4d)
        Commands.setDelegate_of_(self.delegate,self.controller)

        NSNotificationCenter = objc.lookUpClass('NSNotificationCenter')
        notificationCenter = NSNotificationCenter.defaultCenter()
        notificationCenter.addObserver_selector_name_object_(self, "receivedNotification:", "bluetoothHCIEventNotificationMessage", None)

        bytes2 = ''.join(chr(x) for x in [0x4D, 0xFC, 0xF0, 0x31, 0x00, 0x00, 0x00, 0xFB])
        self.sendCommandToIOB(bytes2)
        date = NSDate.dateWithTimeIntervalSinceNow_(2)
        NSRunLoop.currentRunLoop().runUntilDate_(date)
        # x = threading.Thread(target=self.sendCommandToIOB, args=(bytes2,))
        # x.setDaemon(False)
        # x.start()

        return True

    def _recvThreadFunc(self):

        log.debug("Receive Thread started.")

        while not self.exit_requested:
            # Little bit ugly: need to re-apply changes to the global context to the thread-copy
            context.log_level = self.log_level

            # read record data
            try:
                self.s_snoop.listen(1)
                conn, addr = self.s_snoop.accept()
                record_data = conn.recv(1024)
            except socket.timeout:
                continue # this is ok. just try again without error

            # Put all relevant infos into a tuple. The HCI packet is parsed with the help of hci.py.
            record = (hci.parse_hci_packet(record_data), 0, 0, 0, 0, 0) #TODO not sure if this causes trouble?
            # Put the record into all queues of registeredHciRecvQueues if their
            # filter function matches.
            for queue, filter_function in self.registeredHciRecvQueues: # TODO filter_function not working with bluez modifications
                try:
                    queue.put(record, block=False)
                    log.info("recvThreadFunc: Recv queue was not full.")
                except Queue.Full:
                	log.warn("recvThreadFunc: A recv queue is full. dropping packets..>" + record_data)

            # Call all callback functions inside registeredHciCallbacks and pass the
            # record as argument.
            for callback in self.registeredHciCallbacks:
                callback(record)

        log.debug("Receive Thread terminated.")

    def sendCommandToIOB(self, command):
        Commands.sendArbitraryCommand4_len_(command, 0xF0)
        # NSRunLoop.currentRunLoop().run()

    def _sendThreadFunc(self):
    	log.debug("Send Thread started.")
        while not self.exit_requested:
            # Little bit ugly: need to re-apply changes to the global context to the thread-copy
            context.log_level = self.log_level

            # Wait for 'send task' in send queue
            try:
                task = self.sendQueue.get(timeout=0.5)
            except Queue.Empty:
                continue

            # Extract the components of the task
            h4type, data, queue, filter_function = task

            # Prepend UART TYPE and length.
            out = p8(h4type) + data

            # Send command to the chip using IOBluetoothExtended framework
            h4type, data, queue, filter_function = task
            opcode = binascii.hexlify(data[1]) + binascii.hexlify(data[0])
            self.delegate.setWaitingFor_(int(opcode, 16))
            log.info("Sending command: 0x" + binascii.hexlify(data) + ", opcode: " + opcode)

            # if the caller expects a response: register a queue to receive the response
            if queue != None and filter_function != None:
                recvQueue = Queue.Queue(1)
                self.registerHciRecvQueue(recvQueue, filter_function)


            # TODO: SEND!!
            bytes2 = ''.join(chr(x) for x in [0x4D, 0xFC, 0xF0, 0x31, 0x00, 0x00, 0x00, 0xFB])
            self.sendCommandToIOB(bytes2)
            date = NSDate.dateWithTimeIntervalSinceNow_(4)
            NSRunLoop.currentRunLoop().runUntilDate_(date)

            # x = threading.Thread(target=self.sendCommandToIOB, args=(bytes2,))
            # x.setDaemon(True)
            # x.start()

            # this goes into receivedNotification
            if self.s_inject.getsockname()[1] == 0:
                self.s_inject.connect(('127.0.0.1', self.hciport))
            self.s_inject.sendall("040E0C01011000066724060f009641".decode("hex"))

            # if the caller expects a response:
            # Wait for the HCI event response by polling the recvQueue
            if queue != None and filter_function != None:
                try:
                    # record_data = "040E0C01011000066724060f009641".decode("hex")
                    # data = hci.parse_hci_packet(record_data).data
                    record = recvQueue.get(timeout=10)
                    hcipkt = record[0]
                    data   = hcipkt.data
                except Queue.Empty:
                    log.warn("_sendThreadFunc: No response from the firmware.")
                    data = None
                    self.unregisterHciRecvQueue(recvQueue)
                    continue

                queue.put(data)
                self.unregisterHciRecvQueue(recvQueue)

        log.debug("Send Thread terminated.")

    def _teardownSockets(self):
    	if (self.s_inject != None):
            self.s_inject.close()
            self.s_inject = None

        if (self.s_snoop != None):
            self.s_snoop.close()
            self.s_snoop = None

        return True

