import pygatt
import threading
from binascii import hexlify
import binascii
import time
import sys
import typing as T
from BatteryInterface import BALBattery, BatteryStatus
from Interface import Interface, BLEConnection
from BOSErr import NoBattery
# import uuid
# import threading
DEBUG = True

class JBDBMS(BALBattery): 
    dev_info_str = b'\xdd\xa5\x03\x00\xff\xfd\x77'
    bat_info_str = b'\xdd\xa5\x04\x00\xff\xfc\x77'
    ver_info_str = b'\xdd\xa5\x05\x00\xff\xfb\x77'

    enter_factory_mode_register = b'\x00'
    exit_factory_mode_register = b'\x01'
    enter_factory_mode_command = b'\x56\x78'
    exit_and_save_factory_mode_command = b'\x28\x28'
    exit_factory_mode_command = b'\x00\x00'

    def __init__(self, name, addr, staleness=100):
        super().__init__(name, Interface.BLE, addr, staleness)
        self.bms_service_uuid = ('0000ff00-0000-1000-8000-00805f9b34fb') # ("ff00") # 
        self.bms_write_uuid = ('0000ff02-0000-1000-8000-00805f9b34fb') # ("ff02") # 
        self.bms_read_uuid = ('0000ff01-0000-1000-8000-00805f9b34fb') # ("ff01") # 

        self.address = addr
        self.connection = BLEConnection(self.address, self.bms_write_uuid, self.bms_read_uuid)

        if DEBUG: print("connecting")
        self.connection.connect()

        if not self.connection.is_connected(): 
            print("JBDBMS: BLE connection failed!", file=sys.stderr)
            raise NoBattery()

        self.state = self.get_basic_info()

        self.current_range = (120, 120)
        self._current_range = range(-120, 120)
        self.staleness = staleness
        self.last_refresh = (time.time() * 1000)  # in ms

    @staticmethod
    def type() -> str: return "JBDBMS"

    def query_info(self, to_send) -> bytes:
        return self.connection.write(to_send)

    def write_without_receive(self, to_send): 
        self.connection.write_without_receive(to_send)
        time.sleep(0.1)
    
    def close(self): 
        self.connection.close()

    @staticmethod
    def get_read_register_command(register_address) -> bytes: 
        starter = b'\xdd'
        read_indicator = b'\xa5'
        write_indicator = b'\x5a'
        ending = b'\x77'
        twobytes_mask = int('0xffff', base=16)

        reg_addr = register_address
        data_length = b'\x00'
        checksum = (
            ~((int.from_bytes(reg_addr, byteorder='big', signed=False) + 
                int.from_bytes(data_length, byteorder='big', signed=False)) & twobytes_mask)
        ) + 1 
        checksum = (checksum & twobytes_mask).to_bytes(2, 'big', signed=False)
        command = starter + read_indicator + reg_addr + data_length + checksum + ending
        return command

    @staticmethod
    def get_write_register_command(register_address, bytes_to_write) -> bytes: 
        starter = b'\xdd'
        read_indicator = b'\xa5'
        write_indicator = b'\x5a'
        ending = b'\x77'
        twobytes_mask = int('0xffff', base=16)

        reg_addr = register_address
        data_length = len(bytes_to_write).to_bytes(1, byteorder='big', signed=False)
        sum_of_databytes = (sum(bytes_to_write)) 
        checksum = (
            ~((int.from_bytes(reg_addr, byteorder='big', signed=False) + 
                int.from_bytes(data_length, byteorder='big', signed=False) + 
                sum_of_databytes
            ) & twobytes_mask)
        ) + 1 
        checksum = (checksum & twobytes_mask).to_bytes(2, 'big', signed=False)
        command = starter + write_indicator + reg_addr + data_length + bytes_to_write + checksum + ending
        return command

    @staticmethod
    def extract_received_data(info) -> T.Tuple[bool, int, bytes]: 
        """
        info = 0xdd <register_addr> <OK:0x00 / Error:0x80> <length> <data> <checksum> 0x77
        """
        start = info[0]
        end = info[-1]
        checksum = info[-3:-1]
        reg_addr = info[1]
        success = (info[2] == 0) 
        length = info[3]
        data = info[4:-3]
        return (success, length, data)

    def get_basic_info(self) -> T.Dict[str, T.Union[int, bytes]]: 
        info = self.query_info(self.get_read_register_command(b'\x03'))
        byte_order = "big"
        basic_info = {
            "voltage": int.from_bytes(info[4:6], byte_order), 
            "current": int.from_bytes(info[6:8], byte_order), 
            "remaining charge": int.from_bytes(info[8:10], byte_order), 
            "maximum capacity": int.from_bytes(info[10:12], byte_order),
            "cycles": int.from_bytes(info[12:14], byte_order), 
            "manufacture": int.from_bytes(info[14:16], byte_order), 
            "balancing bit 1": info[16:18], 
            "balancing bit 2": info[18:20], 
            "protection state": info[20:22], 
            "version": info[22], 
            "remaining percentage": info[23], 
            "MOSFET status": info[24], 
            "num batteries": info[25], 
            "num temperature sensors": info[26], 
            "temperature information": info[27:-3], 
        }
        return basic_info

    def get_battery_voltages(self):
        voltages = []
        info = self.query_info(self.get_read_register_command(b'\x04'))
        byte_order = "big"
        assert (info[3] % 2 == 0)
        num_batteries = info[3] // 2
        print("#Batteries {}".format(num_batteries))
        for i in range(num_batteries): 
            voltages.append(int.from_bytes(info[4+2*i:4+2*i+2], byte_order))
            # print("Battery {} voltage: {}mV".format(i+1, int.from_bytes(info[4+2*i:4+2*i+2], byte_order)))
        return voltages
    
    def set_max_staleness(self, ms):
        """
        the maximum stale time of a value 
        """
        if ms >= 0: 
            self.staleness = ms

    def check_staleness(self):
        now = (time.time() * 1000)
        if now - self.last_refresh > self.staleness: 
            self.refresh()
            self.last_refresh = (time.time() * 1000)
        return

    def refresh(self):
        self.state = self.get_basic_info()
        self.staleness = time.time()
        if self.state['remaining charge'] <= 0: 
            self._set_dischargeable(False)
        else: 
            self._set_dischargeable(True)
        if self.state['remaining charge'] >= self.get_maximum_capacity(): 
            self._set_chargeable(False)
        else: 
            self._set_chargeable(True)
        newcurrent = self.state['current']
        self.update_meter(newcurrent, newcurrent)

    def get_voltage(self):
        with self._lock:
            self.check_staleness()
            return self.state['voltage']

    def get_current(self):
        with self._lock:
            self.check_staleness()
            return self.state['current']
    
    def get_maximum_capacity(self):
        with self._lock:
            self.check_staleness()
            return self.state['maximum capacity']

    def get_current_capacity(self):
        with self._lock:
            self.check_staleness()
            return self.state['remaining charge']

    def _set_chargeable(self, is_chargeable):
        # self.check_staleness()
        mosfet_status = self.state['MOSFET status']
        assert mosfet_status <= 3
        if is_chargeable: 
            mosfet_status = (mosfet_status & 2)
        else: 
            mosfet_status = (mosfet_status | 1)
        self.query_info(self.get_write_register_command(b'\xe1', mosfet_status.to_bytes(1, byteorder='big')))
    
    def _set_dischargeable(self, is_dischargeable): 
        # self.check_staleness()
        mosfet_status = self.state['MOSFET status']
        assert mosfet_status <= 3
        if is_dischargeable: 
            mosfet_status = (mosfet_status & 1)
        else: 
            mosfet_status = (mosfet_status | 2)
        self.query_info(self.get_write_register_command(b'\xe1', mosfet_status.to_bytes(1, byteorder='big')))

    # def get_mosfet_status_data(self, chargeable, dischargeable): 
    #     mosfet_status = self.state['MOSFET status']
    #     assert mosfet_status <= 3
    #     if chargeable: 
    #         mosfet_status = (mosfet_status & 2)
    #     else: 
    #         mosfet_status = (mosfet_status | 1)
    #     if dischargeable: 
    #         mosfet_status = (mosfet_status & 1)
    #     else: 
    #         mosfet_status = (mosfet_status | 2)
    #     return mosfet_status

    def set_current(self, target_current):
        with self._lock:
            if (target_current not in self._current_range): 
                return False
            if (target_current > 0 and self.get_current_capacity() <= 0): 
                # self._set_dischargeable(False)
                return False
            if (target_current < 0 and self.get_current_capacity() >= self.get_maximum_capacity()): 
                # self._set_chargeable(False)
                return False 
            # if target_current > 0: 
            #     # self._set_chargeable(False)
            #     self._set_dischargeable(True)
            # elif target_current < 0: 
            #     self._set_chargeable(True)
            #     # self._set_dischargeable(False)
            # else: 
            #     self._set_chargeable(False)
            #     self._set_dischargeable(False)
            # how to limit the current? 
            
            return True

    def get_current_range(self):
        with self._lock:
            """
            Max discharging current, Max charging current 
            """
            return self.current_range

    def get_status(self) -> BatteryStatus:
        with self._lock:
            self.check_staleness()
            return BatteryStatus(\
                self.state['voltage'], 
                self.state['current'], 
                self.state['remaining charge'], 
                self.state['maximum capacity'], 
                self.current_range[0], 
                self.current_range[1])
        
# print(
#     hexlify(JBDBMS.get_read_register_command(b'\x03')), 
#     hexlify(JBDBMS.get_read_register_command(b'\x04')), 
#     hexlify(JBDBMS.get_read_register_command(b'\x05')),  
#     hexlify(JBDBMS.get_write_register_command(b'\xe1', b'\x00\x02'))
# )
# exit(0)


def test_query_info(bms: JBDBMS): 
    dev_info_str = JBDBMS.get_read_register_command(b'\x03') # b'\xdd\xa5\x03\x00\xff\xfd\x77'
    bat_info_str = JBDBMS.get_read_register_command(b'\x04') # b'\xdd\xa5\x04\x00\xff\xfc\x77'
    ver_info_str = JBDBMS.get_read_register_command(b'\x05') # b'\xdd\xa5\x05\x00\xff\xfb\x77'
    if DEBUG: print("------ querying device information ------")
    info = bms.query_info(dev_info_str)
    # print(hexlify(info))
    byte_order = "big"
    print(
        "Total Voltage (10mV): {}".format(int.from_bytes(info[4:6], byte_order)), 
        "Current (10mA): {}".format(int.from_bytes(info[6:8], byte_order)), 
        "Remaining charge (10mAh): {}".format(int.from_bytes(info[8:10], byte_order)), 
        "Claimed capacity (10mAh): {}".format(int.from_bytes(info[10:12], byte_order)),
        "Cycles: {}".format(int.from_bytes(info[12:14], byte_order)), 
        "Manufacture: {}".format(int.from_bytes(info[14:16], byte_order)), 
        "Balancing bits: {}".format(hexlify(info[16:18])), 
        "Balancing bits2: {}".format(hexlify(info[18:20])), 
        "Protection state: {}".format(hexlify(info[20:22])), 
        "Version: {}".format(info[22]), 
        "RSOC (remaining charge percentage): {}".format(info[23]), 
        "MOSFET status (bit0: charging0/discharging1?, bit1: turned off0/on1): {}".format(bin(info[24])), 
        "Batteries: {}".format(info[25]), 
        "NTC (number of temperature sensors): {}".format(info[26]), 
        "Temperature information not decoded... {}".format(info[27:-3]), 
        sep='\n'
    )

    if DEBUG: print("------ querying batteries information ------")
    info = bms.query_info(bat_info_str)
    # print(hexlify(info))
    assert (info[3] % 2 == 0)
    num_batteries = info[3] // 2
    print("#Batteries {}".format(num_batteries))
    for i in range(num_batteries): 
        print("Battery {} voltage: {}mV".format(i+1, int.from_bytes(info[4+2*i:4+2*i+2], byte_order)))
    
    if DEBUG: print("------ querying version information ------")
    info = bms.query_info(ver_info_str)
    print("version in hex: ", hexlify(info))


def test_factory_mode(bms: JBDBMS): 
    print("------ Entering factory mode ------")
    info = bms.query_info(
        JBDBMS.get_write_register_command(
            JBDBMS.enter_factory_mode_register, 
            JBDBMS.enter_factory_mode_command))
    print(hexlify(info))

    print("------ Try reading information ------")
    info = bms.query_info(
        JBDBMS.get_read_register_command(b'\x10'))
    success, length, data = JBDBMS.extract_received_data(info)
    print('design capacity', length, int.from_bytes(data, 'big'))

    info = bms.query_info(
        JBDBMS.get_read_register_command(b'\x11'))
    success, length, data = JBDBMS.extract_received_data(info)
    print('capacity per cycle', length, int.from_bytes(data, 'big'))


    info = bms.query_info(
        JBDBMS.get_read_register_command(b'\x12'))
    success, length, data = JBDBMS.extract_received_data(info)
    print('Cell capacity estimate voltage, 100%', length, int.from_bytes(data, 'big'))

    info = bms.query_info(
        JBDBMS.get_read_register_command(b'\x32'))
    success, length, data = JBDBMS.extract_received_data(info)
    print('Cell capacity estimate voltage, 80%', length, int.from_bytes(data, 'big'))

    info = bms.query_info(
        JBDBMS.get_read_register_command(b'\x33'))
    success, length, data = JBDBMS.extract_received_data(info)
    print('Cell capacity estimate voltage, 60%', length, int.from_bytes(data, 'big'))

    info = bms.query_info(
        JBDBMS.get_read_register_command(b'\x34'))
    success, length, data = JBDBMS.extract_received_data(info)
    print('Cell capacity estimate voltage, 40%', length, int.from_bytes(data, 'big'))

    info = bms.query_info(
        JBDBMS.get_read_register_command(b'\x35'))
    success, length, data = JBDBMS.extract_received_data(info)
    print('Cell capacity estimate voltage, 20%', length, int.from_bytes(data, 'big'))

    info = bms.query_info(
        JBDBMS.get_read_register_command(b'\x13'))
    success, length, data = JBDBMS.extract_received_data(info)
    print('Cell capacity estimate voltage, 0%', length, int.from_bytes(data, 'big'))

    print("------ Exiting factory mode ------")
    info = bms.query_info(
        JBDBMS.get_write_register_command(
            JBDBMS.exit_factory_mode_register, 
            JBDBMS.exit_factory_mode_command))
    print(hexlify(info))

if __name__ == "__main__":
    bms = None
    try:
        address = "A4:C1:38:3C:9D:22"
        bms = JBDBMS("JBDBMSTest", address, staleness=1000)
        for _ in range(2): 
            try: 
                # test_query_info(bms)
                # test_factory_mode(bms)
                
                print(bms.state)
                print(bms.get_status())
                print(bms)
                
                break
            except pygatt.device.exceptions.BLEError: 
                # bms.close()
                print("Retrying in 1 second(s)...")
                time.sleep(1)
                continue

        lock = threading.Lock()
        bms.start_background_refresh()
        time.sleep(10)
        bms.stop_background_refresh()
    
    finally:
        if bms != None:
            bms.close()
    
    exit(0)

