import threading
import signal
import time
import json
import typing as T
from BatteryInterface import *
from BOSNode import *
from JBDBMS import JBDBMS
import Interface
from Interface import BLEConnection
from Interface import Connection
from util import *
import BOSErr
from Policy import *

class NullBattery(Battery):
    def __init__(self, name, voltage):
        super().__init__(name)
        self._voltage = voltage

    def refresh(self):
        pass

    def get_status(self):
        return BatteryStatus(self._voltage, 0, 0, 0, 0, 0)

    def set_current(self, current):
        if current != 0:
            raise BOSErr.CurrentOutOfRange
        
    @staticmethod
    def type(): return "Null"
    
    _KEY_VOLTAGE = "voltage"
    
    def serialize(self):
        return self._serialize_base({self._KEY_VOLTAGE: self._voltage})

    @staticmethod
    def _deserialize_derived(d: dict):
        return NullBattery(d[NullBattery._KEY_VOLTAGE])

class PseudoBattery(BALBattery):
    def __init__(self, name, iface, addr, status: BatteryStatus):
        super().__init__(name, iface, addr)
        self._status = status
        self.set_meter(self._status.state_of_charge)

    def __str__(self): return str(self.serialize())
        
    @staticmethod
    def type(): return "Pseudo"

    def get_status(self):
        with self._lock:
            old_meter = self.get_meter()
            self.update_meter(self._status.current, self._status.current) # update meter
            new_meter = self.get_meter()
            self._status.state_of_charge += new_meter - old_meter
            # self._status.state_of_charge = self.get_meter() # TODO: Why did I add this?
            return self._status

    def set_current(self, target_current):
        with self._lock:
            old_current = self.get_status().current
            if not (target_current >= -self._status.max_charging_current and
                    target_current <= self._status.max_discharging_current):
                raise BOSErr.CurrentOutOfRange
            self._status.current = target_current
            new_current = self.get_status().current
            self.update_meter(old_current, new_current)

    def set_status(self, status):
        with self._lock:
            self.update_meter(self._status.current, status.current)
            self._status = status

    _KEY_IFACE = "iface"
    _KEY_ADDR = "addr"
    _KEY_STATUS = "status"

    def serialize(self):
        with self._lock:
            return self._serialize_base({self._KEY_STATUS: self._status.serialize()})

    @staticmethod
    def _deserialize_derived(d):
        return PseudoBattery(d[PseudoBattery._KEY_IFACE], d[PseudoBattery._KEY_ADDR],
                             d[PseudoBattery._KEY_STATUS])


class NetworkBattery(Battery):
    def __init__(self, name, remote_name, iface, addr, *args):
        super().__init__(name)
        self._remote_name = remote_name
        self._conn = Connection.create(iface, addr, *args)
        self._conn.connect()
        self._status = None    # Cached status
        self._timestamp = None
        self.refresh()

    def _send_recv(self, req):
        req_bytes = bytes(json.dumps(req), 'ASCII')
        self._conn.write(req_bytes)
        resp_str = self._conn.readstr()
        resp = json.loads(resp_str)
        return resp

    def _resp_get_body(self, resp):
        if 'response' not in resp:
            raise BOSErr.BadResponse(resp)
        body = resp['response']
        if body is None and 'error' in resp:
            error = resp['error']
            print(f'{self._name}: server error; {error}')
            raise BOSErr.ServerError(error)
        return body

    def refresh(self):
        with self._lock: 
            req = {
                'request': 'get_status',
                'name': self._remote_name,
            }
            resp = self._send_recv(req)
            self._status = BatteryStatus.from_json(self._resp_get_body(resp))
            # self._status = self._resp_get_body(resp)
            self._timestamp = time.time() # TODO: need to set true timestamp.
            current = self._status.current
            self.update_meter(current, current)

    def set_current(self, target_current):
        with self._lock:
            old_current = self._status.current
            req = {
                'request': 'set_current',
                'name': self._remote_name,
                'current': target_current,
            }
            resp = self._send_recv(req)
            ok = self._resp_get_body(resp)
            new_current = self.get_current() # TODO: this is expensive
            self.update_meter(old_current, new_current)

    def get_status(self):
        with self._lock:
            if time.time() - self._timestamp >= self._sample_period:
                self.refresh()
            self.update_meter(self._status.current, self._status.current)
        return self._status
        
class AggregatorBattery(Battery):
    def __init__(self, name, voltage, voltage_tolerance, srcnames: T.List[str], lookup):
        '''
        `voltage` is the reported voltage of the aggregated battery. 
        `voltage_tolerance` indicates how much the source voltages are allowed to differ from the 
        virtual voltage.
        `srcnames` is a list of battery names (strings)
        `lookup` is a function that resolves a battery name into a battery object.
        '''
        super().__init__(name)
        self._lookup = lookup
        self._srcnames = srcnames
        self._voltage = voltage
        self._voltage_tolerance = voltage_tolerance

        if len(srcnames) == 0:
            raise BOSErr.NoBattery

        # check voltages within given tolerance
        for srcname in srcnames:
            source = self._lookup(srcname)
            v = source.get_voltage()
            if not (v >= voltage - voltage_tolerance and v <= voltage + voltage_tolerance):
                raise BOSErr.VoltageMismatch

    def refresh(self):
        # for srcname in self._srcnames:
        #    self._lookup(srcname).refresh()
        # TODO: Check to make sure voltages still in range.
        current = self.get_current()
        self.update_meter(current, current)

    def get_status(self):
        s_max_capacity = 0
        s_cur_capacity = 0
        s_max_discharge_time = 0
        s_max_charge_time = 0
        s_current = 0

        for srcname in self._srcnames:
            source = self._lookup(srcname)
            s_max_capacity += source.get_maximum_capacity()
            s_cur_capacity += source.get_state_of_charge()
            (max_discharge_rate, max_charge_rate) = source.get_current_range()
            s_max_discharge_time = max(s_max_discharge_time,
                                       source.get_state_of_charge() / max_discharge_rate)
            s_max_charge_time = max(s_max_charge_time, (source.get_maximum_capacity() -
                                                        source.get_state_of_charge()) /
                                    max_charge_rate)
            s_current += source.get_current()

        s_max_discharge_rate = s_cur_capacity / s_max_discharge_time
        s_max_charge_rate = (s_max_capacity - s_cur_capacity) / s_max_charge_time

        # update meter
        self.update_meter(s_current, s_current)

        return BatteryStatus(self._voltage,
                             s_current,
                             s_cur_capacity,
                             s_max_capacity,
                             s_max_discharge_rate,
                             s_max_charge_rate)

    def set_current(self, target_current):
        if verbose:
            print(f'set current {target_current}')
        
        old_current = self.get_status().current
        
        (max_discharge_rate, max_charge_rate) = self.get_current_range()
        if not (target_current >= -max_charge_rate and target_current <= max_discharge_rate):
            raise BOSErr.CurrentOutOfRange

        sources = [self._lookup(srcname) for srcname in self._srcnames]

        if target_current > 0: # discharging
            charge = self.get_state_of_charge()
            time = charge / target_current
            for source in sources:
                current = source.get_state_of_charge() / time
                source.set_current(current)
        elif target_current < 0: # charging
            charge = self.get_maximum_capacity() - self.get_state_of_charge()
            time = charge / -target_current
            for source in sources:
                current = (source.get_maximum_capacity() - source.get_state_of_charge()) / time
                source.set_current(current)
        else: # none
            for source in sources:
                source.set_current(0)

        new_current = self.get_status().current
        self.update_meter(old_current, new_current)
        

    @staticmethod
    def type(): return "Aggregator"

    _KEY_SOURCES = "sources"
    _KEY_VOLTAGE = "voltage"
    _KEY_VOLTAGE_TOLERANCE = "voltage_tolerance"
    
    def serialize(self):
        return self._serialize_base({self._KEY_SOURCES: self._srcnames,
                                     self._KEY_VOLTAGE: self._voltage,
                                     self._KEY_VOLTAGE_TOLERANCE: self._voltage_tolerance})

    @staticmethod
    def _deserialize_derived(d: dict, lookup):
        return AggregatorBattery(d[AggregatorBattery._KEY_VOLTAGE],
                                 d[AggregatorBattery._KEY_VOLTAGE_TOLERANCE],
                                 d[AggregatorBattery._KEY_SOURCES],
                                 lookup)
                                 

class SplitterBattery(Battery):
    '''
    The splitter battery delegates most of the work to its associated splitter policy.
    '''
    
    def __init__(self, name, sample_period, policyname, lookup):
        super().__init__(name)
        self._sample_period = sample_period
        self._policyname = policyname
        self._lookup = lookup

    @staticmethod
    def type(): return "Splitter"

    def _policy(self): return self._lookup(self._policyname)
                     
    def refresh(self):
        self._policy().refresh()
        current = self.get_current()
        self.update_meter(current, current)
        
    def get_status(self):
        status = self._policy().get_status(self._name)
        self.update_meter(status.current, status.current)
        return status

    def set_current(self, target_current):
        old_current = self.get_status().current
        self._policy().set_current(self._name, target_current)
        new_current = self.get_status().current
        self.update_meter(old_current, new_current)

    def reset_meter(self):
        self._policy().reset_meter(self._name)
    

# class NetworkedBattery:
#     def __init__(self, name, iface, addr, *args):
#         if iface == Interface.TCP:
#             if len(args) == 1:
#                 raise BOSErr.InvalidArgument("NetworkedBattery.__init__(): missing additional"\
#                                              " argument 'port'")
#             self._conn = TCPConnection(addr, args[0])
#         else:
#             raise BOSErr.InvalidArgument("Bad interface {}".format(iface))
# 
#         self._conn.connect()
# 
#     @staticmethod
#     def type(): return "Networked"
# 
#     def refresh(self):
#         raise NotImplementedError
# 
#     def get_status(self):
        
            
class BOSDirectory:
    def __init__(self, lock: threading.RLock):
        self._map = dict() # map battery names to (battery, children)
        self._lock = lock

    def __contains__(self, name: str):
        with self._lock:
            return name in self._map

    def __getitem__(self, name):
        with self._lock:
            if name not in self:
                raise BOSErr.BadName(name)
            return self._map[name]

    def __str__(self):
        with self._lock:
            strd = dict([(k, tuple(map(str, v))) for (k, v) in self._map.items()])
            return str(strd)

    def keys(self):
        with self._lock:
            return self._map.keys()

    def add_node(self, name: str, node: BOSNode, parents=set()):
        with self._lock:
            if name in self:
                raise BOSErr.NameTaken
            self._map[name] = (node, parents)

    def remove_node(self, name: str):
        with self._lock:
            raise NotImplementedError

    def get_parents(self, name: str):
        with self._lock:
            return self._map[name][1]

    def get_children(self, name: str):
        with self._lock:
            children = set()
            for child in self._map:
                if name in self.get_parents(child):
                    children.add(child)
            return children


    def replace_node(self, name: str, newnode: BOSNode):
        with self._lock:
            raise NotImplementedError

    def isfree(self, name: str) -> bool:
        return len(self.get_children(name)) == 0

    def isused(self, name: str) -> bool:
        return not self.isfree(name)
            
        


#     def refresh(self):
#         for name in self._directory:
#             node = self._directory[name][0]
#             if isinstance(node, Battery):
#                 node.refresh()
    

    def print_status(self):
        with self._lock:
            for src in self._map:
                node = self._map[src][0]
                print('{}:'.format(src))
                if isinstance(node, Battery):
                    status = node.get_status()
                    print('\tcurrent:         {}A'.format(status.current))
                    print('\tstate of charge: {}Ah'.format(status.state_of_charge))
                    print('\tmax capacity:    {}Ah'.format(status.max_capacity))
                    print('\tmax discharge:   {}A'.format(status.max_discharging_current))
                    print('\tmax charge:      {}A'.format(status.max_charging_current))
                    print('\tmeter:           {}Ah'.format(node.get_meter()))
                elif isinstance(node, SplitterPolicy):
                    status = node.get_splitter_status()
                    print('\tsource: {}'.format(status["source"]))
                    parts = status["parts"]
                    print('\tparts: {}'.format(len(parts)))
                    for part in parts:
                      print('\t\t{}:'.format(part["name"]))
                      print('\t\t\tscale:   {}'.format(part["scale"]))
                      print('\t\t\tcurrent: {}A'.format(part["current"]))

                  
    def visualize(self):
        with self._lock:
            import networkx as nx
            import matplotlib.pyplot as plt
            g = nx.DiGraph()
            for src in self._map:
                for dst in self._map[src][1]:
                    g.add_edge(dst, src)
            nx.draw_networkx(g)
            plt.show()

    
        
class BOS:
    battery_types = dict([(battery.type(), battery) for battery in
                         [NullBattery, PseudoBattery, AggregatorBattery, SplitterBattery,
                          JBDBMS]])
    
    def __init__(self, lock=threading.RLock()):
        # Directory: map from battery names to battery objects
        self._directory = BOSDirectory(lock)
        # self._ble = Interface.BLE()
        self._lookup = lambda name: self._directory[name][0]

    def __str__(self):
        return '{{directory = {}}}'.format(self._directory)


    def list(self):
        return self._directory.keys()

    def _make(self, battery: Battery):
        battery.start_background_refresh()

    def make_null(self, name: str, voltage) -> NullBattery:
        battery = NullBattery(name, voltage)
        battery.reset_meter()
        self._directory.add_node(name, battery)
        return battery
        
        
    def make_battery(self, name: str, kind: str, iface: Interface, addr: str, *args):
        if kind == JBDBMS.type():
            battery = JBDBMS(name, iface, addr, *args)
        else:
            battery_type = self.battery_types[kind]
            battery = battery_type(name, iface, addr, *args)
            battery.reset_meter()
        self._directory.add_node(name, battery)
        return battery

    def make_network(self, name: str, remote_name: str, iface: Interface, addr: str, *args):
        battery = NetworkBattery(name, remote_name, iface, addr, *args)
        self._directory.add_node(name, battery)
        return battery
    
    def make_aggregator(self, name: str, sources: T.List[str], voltage, voltage_tolerance):
        battery = AggregatorBattery(name, voltage, voltage_tolerance, sources, self._lookup)
        battery.reset_meter()
        self._directory.add_node(name, battery, set(sources))
        return battery

    def make_splitter_policy(self, policyname: str, policytype, parent: str, initname: str, *policyargs) -> SplitterPolicy:
        '''
        Make a splitter policy. Initializes the policy with one managed battery.
        policyname: name of policy to create
        policytype: type constructor that will be used to instantiate the battery
        parent: source of the splitter policy
        initname: name of the initialization battery
        *policyargs: subtype-specific arguments to pass to policy constructor
        '''
        if not issubclass(policytype, SplitterPolicy):
            raise BOSErr.InvalidArgument

        policy = policytype(parent, self._lookup, initname, *policyargs)
        self._directory.add_node(policyname, policy, {parent})
        
        # add initial battery
        # TODO: sample period
        init_battery = SplitterBattery(initname, 0, policyname, self._lookup)
        self._directory.add_node(initname, init_battery, {policyname})
        init_battery.reset_meter()
        return policy

    def make_splitter_battery(self, batteryname: str, policyname: str, sample_period, tgt_status,
                              *policyargs):
        battery = SplitterBattery(batteryname, sample_period, policyname, self._lookup)
        self._directory.add_node(batteryname, battery, {policyname})
        policy = self._lookup(policyname)
        policy.add_child(batteryname, tgt_status, *policyargs)
        battery.reset_meter()
        return battery
    

    def get_status(self, name: str) -> BatteryStatus:
        return self._directory[name][0].get_status()

    def set_current(self, name: str, current):
        self.lookup(name).set_current(current)

    def start_background_refresh(self, name: str):
        self.lookup(name).start_background_refresh()

    def stop_background_refresh(self, name: str):
        self.lookup(name).stop_background_refresh()

    def refresh(self, name: str):
        self.lookup(name).refresh()
    
    def free_battery(self, name: str): return self._directory.free_battery(name)
    
    def replace_battery(self, name: str, *args):
        newbattery = self.make_battery(name, *args)
        self._directory.replace_battery(name, newbattery)
        return battery

    def print_status(self):
        self._directory.print_status()
    
    def visualize(self):
        self._directory.visualize()

    def lookup(self, name: str):
        return self._lookup(name)

    def get_parents(self, name: str):
        return self._directory.get_parents(name)

    def get_children(self, name: str):
        return self._directory.get_children(name)

import util
if __name__ == '__main__':
    util.bos_time = DummyTime(0)
    bos = BOS()

    pseudo1 = bos.make_battery("pseudo1", "Pseudo", Interface.BLE, "pseudo1",
                              BatteryStatus(7000, 0, 500, 1000, 12, 12))
    pseudo2 = bos.make_battery("pseudo2", "Pseudo", Interface.BLE, "pseudo2",
                               BatteryStatus(7000, 0, 500, 1000, 12, 12))
    agg = bos.make_aggregator("agg", ["pseudo1", "pseudo2"], 7000, 500, 100)

    # bos.print_status()
    
    agg.set_current(12)

    bos.print_status()

    print('=====')
    
    util.bos_time.tick(3600)

    # splitter
    splitter = bos.make_splitter_policy("splitter", ProportionalPolicy, "agg", "splitter_free")

    # bos.print_status()

    free_status = bos._lookup("splitter_free").get_status()
    bos.make_splitter_battery("part1", "splitter", 0,
                              BatteryStatus(free_status.voltage,
                                            0,
                                            free_status.state_of_charge / 2,
                                            free_status.max_capacity / 2,
                                            free_status.max_discharging_current / 2,
                                            free_status.max_charging_current / 2,
                                            ),
                              "splitter_free",
                              )

    bos.print_status()

    print('=========')

    util.bos_time.tick(3600)

    bos.print_status()
    
    # bos.visualize()
    
