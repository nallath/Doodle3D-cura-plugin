# Copyright (c) 2015 Ultimaker B.V.
# Cura is released under the terms of the AGPLv3 or higher.

from .avr_isp import stk500v2, ispBase, intelHex
import serial
import threading
import time
import queue
import re
import functools
import os
import os.path
import sys

import http.client
import json
import urllib

from UM.Application import Application
from UM.Signal import Signal, SignalEmitter
from UM.Resources import Resources
from UM.Logger import Logger
from UM.OutputDevice.OutputDevice import OutputDevice
from UM.OutputDevice import OutputDeviceError
from UM.PluginRegistry import PluginRegistry

from PyQt5.QtQuick import QQuickView
from PyQt5.QtQml import QQmlComponent, QQmlContext
from PyQt5.QtCore import QUrl, QObject, pyqtSlot, pyqtProperty, pyqtSignal, Qt

from UM.i18n import i18nCatalog
catalog = i18nCatalog("cura")

class PrinterConnection(OutputDevice, QObject, SignalEmitter):
    def __init__(self, serial_port, parent = None):
        QObject.__init__(self, parent)
        OutputDevice.__init__(self, serial_port)
        SignalEmitter.__init__(self)
        #super().__init__(serial_port)
        self.setName(catalog.i18nc("@item:inmenu", "Doodle3D printing"))
        self.setShortDescription(catalog.i18nc("@action:button", "Print with Doodle3D"))
        self.setDescription(catalog.i18nc("@info:tooltip", "Print to Doodle3D WiFi-Box" + " (" + serial_port + ")"))
        self.setIconName("print")

        self._serial = None
        self._serial_port = serial_port
        self._error_state = None

        self._connect_thread = threading.Thread(target = self._connect)
        self._connect_thread.daemon = True

        self._end_stop_thread = threading.Thread(target = self._pollEndStop)
        self._end_stop_thread.daemon = True

        # Printer is connected
        self._is_connected = False

        # Printer is in the process of connecting
        self._is_connecting = False

        # The baud checking is done by sending a number of m105 commands to the printer and waiting for a readable
        # response. If the baudrate is correct, this should make sense, else we get giberish.
        self._required_responses_auto_baud = 3

        self._progress = 0

        self._update_firmware_thread = threading.Thread(target= self._updateFirmware)
        self._update_firmware_thread.daemon = True
        
        self._heatup_wait_start_time = time.time()

        ## Queue for commands that need to be send. Used when command is sent when a print is active.
        self._command_queue = queue.Queue()

        self._is_printing = False

        ## Set when print is started in order to check running time.
        self._print_start_time = None
        self._print_start_time_100 = None

        ## Keep track where in the provided g-code the print is
        self._gcode_position = 0

        # List of gcode lines to be printed
        self._gcode = []

        # Number of extruders
        self._extruder_count = 1

        # Temperatures of all extruders
        self._extruder_temperatures = [0] * self._extruder_count

        # Target temperatures of all extruders
        self._target_extruder_temperatures = [0] * self._extruder_count

        #Target temperature of the bed
        self._target_bed_temperature = 0 

        # Temperature of the bed
        self._bed_temperature = 0

        # Current Z stage location 
        self._current_z = 0

        self._x_min_endstop_pressed = False
        self._y_min_endstop_pressed = False
        self._z_min_endstop_pressed = False

        self._x_max_endstop_pressed = False
        self._y_max_endstop_pressed = False
        self._z_max_endstop_pressed = False

        # In order to keep the connection alive we request the temperature every so often from a different extruder.
        # This index is the extruder we requested data from the last time.
        self._temperature_requested_extruder_index = 0 

        self._updating_firmware = False

        self._firmware_file_name = None

        self._control_view = None

        self._printer_info_thread = threading.Thread(target = self.getPrinterInfo)
        self._printer_info_thread.daemon = True
        self.connectPrinterInfo()

        self._printing_thread = threading.Thread(target = self.printGCode)
        self._printing_thread.daemon = True

    onError = pyqtSignal()
    progressChanged = pyqtSignal()
    extruderTemperatureChanged = pyqtSignal()
    bedTemperatureChanged = pyqtSignal()

    endstopStateChanged = pyqtSignal(str ,bool, arguments = ["key","state"])

    @pyqtProperty(float, notify = progressChanged)
    def progress(self):
        return self._progress

    @pyqtProperty(float, notify = extruderTemperatureChanged)
    def extruderTemperature(self):
        ##self.stateReply = self.get(self._serial_port,"/d3dapi/info/status")
        ##self.extruderTemperatureChanged.emit()
        ##self.extTemperature = self.stateReply['data']['hotend']
        return self.extTemperature
        ##self._extruder_temperatures[0]

    @pyqtProperty(float, notify = bedTemperatureChanged)
    def bedTemperature(self):
        return self._bed_temperature

    @pyqtProperty(str, notify = onError)
    def error(self):
        return self._error_state

    # TODO: Might need to add check that extruders can not be changed when it started printing or loading these settings from settings object    
    def setNumExtuders(self, num):
        self._extruder_count = num
        self._extruder_temperatures = [0] * self._extruder_count
        self._target_extruder_temperatures = [0] * self._extruder_count

    ##  Is the printer actively printing
    def isPrinting(self):
        if not self._is_connected or self._serial is None:
            return False
        return self._is_printing

    def sendGCode(self,gcode,index):
        if index == 0:
            first = 'true'
        else:
            first = 'false'
        gcodeResponse = self.httppost(self._serial_port,"/d3dapi/printer/print",{
            'gcode': gcode,
            'start': first,
            'first': first
        })

        return gcodeResponse

    @pyqtSlot()
    def startPrint(self):
        if self.stateReply['data']['state'] == "idle":
            Logger.log("d", "startPrint wordt uitgevoerd")
            self.writeStarted.emit(self)
            self._is_printing = True
            gcode_list = getattr( Application.getInstance().getController().getScene(), "gcode_list")
            Logger.log("d", "gcode_list is: %s" % gcode_list)
            self._printing_thread.start()
            self.printGCode(gcode_list)
        else:
            pass

    ##  Start a print based on a g-code.
    #   \param gcode_list List with gcode (strings).
    def printGCode(self, gcode_list):
        self.joinedString = "".join(gcode_list)


        self.decodedList = []
        self.decodedList = self.joinedString.split('\n')

        Logger.log("d", "decodedList is: %s" % self.decodedList)

        self.blocks = []
        self.tempBlock = []

        for i in range(len(self.decodedList)):
            self.tempBlock.append(self.decodedList[i])

            if sys.getsizeof(self.tempBlock) > 7000:
                self.blocks.append(self.tempBlock)
                Logger.log("d", "New block, size: %s" % sys.getsizeof(self.tempBlock))
                ##self.getPrinterInfo()
                ##Logger.log("d", "self.extTemperature is: %s" % self.extTemperature)
                self.tempBlock = []
                ##self.setProgress((  / self.totalLines) * 100)
                ##self.progressChanged.emit()

        
        self.blocks.append(self.tempBlock)
        self.tempBlock = []
        
        self.totalLines = self.joinedString.count('\n') - self.joinedString.count('\n;') - len(self.blocks)

        ## Size of the print defined in total lines so we can use it to calculate the progress bar
        Logger.log("d","totalLines is: %s" % self.totalLines)

        for j in range(len(self.blocks)):
            ##Logger.log("d", "Block sending")
            successful = False
            while not successful:
                self.storedGCodeResponse = self.sendGCode('\n'.join(self.blocks[j]),j)
                if self.storedGCodeResponse['status'] == "success":
                    self.storedGCodeResponse = []
                    successful = True     
                else:
                    Logger.log("d","Couldn't send the block")
                    #Send the failed block again after 15 seconds
                    time.sleep(15)

    ##  Get the serial port string of this connection.
    #   \return serial port
    def getSerialPort(self):
        return self._serial_port

    def connectPrinterInfo(self):
        self._printer_info_thread.start()

    ##  Try to connect the serial. This simply starts the thread, which runs _connect.
    def connect(self):
        if not self._updating_firmware and not self._connect_thread.isAlive():
            self._connect_thread.start()

    ##  Private fuction (threaded) that actually uploads the firmware.
    
    def _updateFirmware(self):
        if self._is_connecting or  self._is_connected:
            self.close()
        hex_file = intelHex.readHex(self._firmware_file_name)

        if len(hex_file) == 0:
            Logger.log("e", "Unable to read provided hex file. Could not update firmware")
            return 

        programmer = stk500v2.Stk500v2()
        programmer.progressCallback = self.setProgress 
        programmer.connect(self._serial_port)

        time.sleep(1) # Give programmer some time to connect. Might need more in some cases, but this worked in all tested cases.

        if not programmer.isConnected():
            Logger.log("e", "Unable to connect with serial. Could not update firmware")
            return 

        self._updating_firmware = True

        try:
            programmer.programChip(hex_file)
            self._updating_firmware = False
        except Exception as e:
            Logger.log("e", "Exception while trying to update firmware %s" %e)
            self._updating_firmware = False
            return
        programmer.close()

        self.setProgress(100, 100)

        self.firmwareUpdateComplete.emit()

    ##  Upload new firmware to machine
    #   \param filename full path of firmware file to be uploaded
    def updateFirmware(self, file_name):
        Logger.log("i", "Updating firmware of %s using %s", self._serial_port, file_name)
        self._firmware_file_name = file_name
        self._update_firmware_thread.start()

    @pyqtSlot()
    def startPollEndstop(self):
        self._poll_endstop = True
        self._end_stop_thread.start()


    @pyqtSlot()
    def stopPollEndstop(self):
        self._poll_endstop = False

    def _pollEndStop(self):
        while self._is_connected and self._poll_endstop:
            self.sendCommand("M119")
            time.sleep(0.5)

    ##  Private connect function run by thread. Can be started by calling connect.
    def _connect(self):
        Logger.log("d", "Attempting to connect to %s", self._serial_port)
        self._is_connecting = True
        self.setIsConnected(True)
        Logger.log("i", "Established printer connection on port %s" % self._serial_port)
        return 

        #         self._sendCommand("M105") # Send M105 as long as we are listening, otherwise we end up in an undefined state

        # Logger.log("e", "Baud rate detection for %s failed", self._serial_port)
        # self.close() # Unable to connect, wrap up.
        # self.setIsConnected(False)

    ##  Set the baud rate of the serial. This can cause exceptions, but we simply want to ignore those.
    def setBaudRate(self, baud_rate):
        try:
            self._serial.baudrate = baud_rate
            return True
        except Exception as e:
            return False

    def setIsConnected(self, state):
        self._is_connecting = False
        if self._is_connected != state:
            self._is_connected = state
            self.connectionStateChanged.emit(self._serial_port)
            # if self._is_connected: 
                # self._listen_thread.start() #Start listening
        else:
            Logger.log("w", "Printer connection state was not changed")

    connectionStateChanged = Signal()

    ##  Close the printer connection
    def close(self):
        Logger.log("d", "Closing the printer connection.")
        if self._connect_thread.isAlive():
            try:
                self._connect_thread.join()
            except Exception as e:
                pass # This should work, but it does fail sometimes for some reason

        self._connect_thread = threading.Thread(target=self._connect)
        self._connect_thread.daemon = True
        
        # if self._serial is not None:
        #     self.setIsConnected(False)
        #     try:
        #         self._listen_thread.join()
        #     except:
        #         pass
        # self._serial.close()

        # self._listen_thread = threading.Thread(target=self._listen)
        # self._listen_thread.daemon = True
        # self._serial = None

    def isConnected(self):
        return self._is_connected 

    @pyqtSlot(int)
    def heatupNozzle(self, temperature):
        Logger.log("d", "Setting nozzle temperature to %s", temperature)
        self._sendCommand("M104 S%s" % temperature)

    @pyqtSlot(int)
    def heatupBed(self, temperature):
        Logger.log("d", "Setting bed temperature to %s", temperature)
        self._sendCommand("M140 S%s" % temperature)

    @pyqtSlot("long", "long","long")
    def moveHead(self, x, y, z):
        Logger.log("d","Moving head to %s, %s , %s", x, y, z)
        self._sendCommand("G0 X%s Y%s Z%s"%(x,y,z))

    @pyqtSlot()
    def homeHead(self):
       self._sendCommand("G28")

    ##  Directly send the command, withouth checking connection state (eg; printing).
    #   \param cmd string with g-code
    def _sendCommand(self, cmd):
        # if self._serial is None:
            # return

        if "M109" in cmd or "M190" in cmd:
            self._heatup_wait_start_time = time.time()
        if "M104" in cmd or "M109" in cmd:
            try:
                t = 0
                if "T" in cmd:
                    t = int(re.search("T([0-9]+)", cmd).group(1))
                self._target_extruder_temperatures[t] = float(re.search("S([0-9]+)", cmd).group(1))
            except:
                pass
        if "M140" in cmd or "M190" in cmd:
            try:
                self._target_bed_temperature = float(re.search("S([0-9]+)", cmd).group(1))
            except:
                pass
        try:
            command = (cmd + "\n").encode()
            Logger.log("d","TODO: send gcode to server: %s" % cmd)
            # self._serial.write(b"\n")
            # self._serial.write(command)
        except serial.SerialTimeoutException:
            Logger.log("w","Serial timeout while writing to serial port, trying again.")
            try:
                time.sleep(0.5)
                # self._serial.write((cmd + "\n").encode())
            except Exception as e:
                Logger.log("e","Unexpected error while writing serial port %s " % e)
                self._setErrorState("Unexpected error while writing serial port %s " % e)
                self.close()
        except Exception as e:
            Logger.log("e","Unexpected error while writing serial port %s" % e)
            self._setErrorState("Unexpected error while writing serial port %s " % e)
            self.close()

    ##  Ensure that close gets called when object is destroyed
    def __del__(self):
        self.close()

    def createControlInterface(self):
        if self._control_view is None:
            Logger.log("d", "Creating control interface for printer connection")
            path = QUrl.fromLocalFile(os.path.join(PluginRegistry.getInstance().getPluginPath("Doodle3D"), "ControlWindow.qml"))
            component = QQmlComponent(Application.getInstance()._engine, path)
            self._control_context = QQmlContext(Application.getInstance()._engine.rootContext())
            self._control_context.setContextProperty("manager", self)
            self._control_view = component.create(self._control_context)

    ##  Show control interface.
    #   This will create the view if its not already created.
    def showControlInterface(self):
        if self._control_view is None:
            self.createControlInterface()
        self._control_view.show()

    ##  Send a command to printer. 
    #   \param cmd string with g-code
    def sendCommand(self, cmd):
        if self.isPrinting():
            self._command_queue.put(cmd)
        elif self.isConnected():
            self._sendCommand(cmd)

    ##  Set the error state with a message.
    #   \param error String with the error message.
    def _setErrorState(self, error):
        self._error_state = error
        self.onError.emit()

    ##  Private function to set the temperature of an extruder
    #   \param index index of the extruder
    #   \param temperature recieved temperature
    def _setExtruderTemperature(self, index, temperature):
        try: 
            ##self._extruder_temperatures[index] = temperature
            self.extruderTemperatureChanged.emit()
        except Exception as e:
            pass

    ##  Private function to set the temperature of the bed.
    #   As all printers (as of time of writing) only support a single heated bed,
    #   these are not indexed as with extruders.
    def _setBedTemperature(self, temperature):
        self._bed_temperature = temperature
        self.bedTemperatureChanged.emit()

    def requestWrite(self, node, file_name = None):
        self.showControlInterface()

    ##  Set the progress of the print. 
    #   It will be normalized (based on max_progress) to range 0 - 100
    def setProgress(self, progress, max_progress = 100):
        self._progress  = (progress / max_progress) * 100 #Convert to scale of 0-100
        self.progressChanged.emit()
    
    ##  Cancel the current print. Printer connection wil continue to listen.
    @pyqtSlot()
    def cancelPrint(self):
        self.httppost(self._serial_port,"/d3dapi/printer/stop",{
            'gcode': 'M104 S0\nG28'
        })
        ## Turn of temperatures
        ## self._sendCommand("M104 S0")
        self._is_printing = False

    ##  Check if the process did not encounter an error yet.
    def hasError(self):
        return self._error_state != None

    ##  private read line used by printer connection to listen for data on serial port.
    def _readline(self):
        if self._serial is None:
            return None
        try:
            ret = self._serial.readline()
        except Exception as e:
            Logger.log("e","Unexpected error while reading serial port. %s" %e)
            self._setErrorState("Printer has been disconnected") 
            self.close()
            return None
        return ret

    ##  Create a list of baud rates at which we can communicate.
    #   \return list of int
    def _getBaudrateList(self):
        ret = [115200, 250000, 230400, 57600, 38400, 19200, 9600]
        return ret

    def _onFirmwareUpdateComplete(self):
        self._update_firmware_thread.join()
        self._update_firmware_thread = threading.Thread(target= self._updateFirmware)
        self._update_firmware_thread.daemon = True

        self.connect()


    def httppost(self,domain,path,data):
        params = urllib.parse.urlencode(data)
        headers = {
        "Content-type": "x-www-form-urlencoded", 
        "Accept": "text/plain", 
        "User-Agent": "Cura Doodle3D connection"
        }

        connect = http.client.HTTPConnection(domain)
        connect.request("POST", path, params, headers)

        response = connect.getresponse()
        jsonresponse = response.read()
        return json.loads(jsonresponse.decode())

    def getPrinterInfo(self):
        while True:
            self.stateReply = self.get(self._serial_port,"/d3dapi/info/status")
            Logger.log("d", "stateReply is: %s" % self.stateReply)
            ##Get Extruder Temperature and emit it to the pyqt framework
            
            if self.stateReply['data']['hotend']:
                self.extTemperature = self.stateReply['data']['hotend']
                self.extruderTemperatureChanged.emit()
            else:
                continue
            
            ##Get currentLine in printing and emit it to the pyqt framework
            ##if self.stateReply['data']['state'] != "idle" or self.stateReply['data']['state'] != "disconnected":
            if self.stateReply['data']['state'] == "printing":
                self.currentLine = self.stateReply['data']['current_line']
                Logger.log("d", "currentLine is: %s" % self.currentLine)
                Logger.log("d", "totalLines is: %s" % self.totalLines)
                self.setProgress((self.currentLine / self.totalLines) * 100)
                time.sleep(2)
            else:
                ##wait 5 seconds before updating info
                self.setProgress(0)
                time.sleep(2)
            
            

    def get (self,domain,path):
        connect = http.client.HTTPConnection(domain)
        connect.request("GET", path)
        response = connect.getresponse()
        jsonresponse = response.read()
        return json.loads(jsonresponse.decode())