import json
import multiprocessing
import os
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

from usb_portal import usbip
from usb_portal.cli import client_menu
from usb_portal.client import ActiveConnection, ClientSession, _resolve_auto_connect_devices
from usb_portal.daemon import BoundDevice, DaemonState, GamepadDaemon
from usb_portal.discovery import DiscoveryScanner, RemoteHost, private_machine_id, scan_cidr
from usb_portal.saved_hosts import AutoConnectDevice, SavedHost, SavedHostsConfig


def write_hosts(path, barrier, number):
    c = SavedHostsConfig(Path(path))
    barrier.wait(timeout=10)
    for i in range(5):
        if not c.save_host(SavedHost(ip=f'192.0.2.{number*5+i+1}')):
            raise RuntimeError('save failed')


class PersistenceTests(unittest.TestCase):
    def test_first_time_writers_merge_devices_for_same_host(self):
        with tempfile.TemporaryDirectory() as tmp:
            c=SavedHostsConfig(Path(tmp)/'hosts.json')
            first=SavedHost(ip='192.0.2.1',auto_connect=[AutoConnectDevice('1-1','0079','1804')])
            second=SavedHost(ip='192.0.2.1',auto_connect=[AutoConnectDevice('1-2','0079','1804')])
            c.save_host(first);c.save_host(second)
            self.assertEqual([d.busid for d in c.get(first.ip).auto_connect],['1-1','1-2'])

    def test_process_writers_preserve_all_updates(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'hosts.json'
            context = multiprocessing.get_context('fork')
            barrier = context.Barrier(4)
            workers = [context.Process(target=write_hosts, args=(str(path),barrier,n)) for n in range(4)]
            for worker in workers: worker.start()
            for worker in workers:
                worker.join(timeout=15)
                self.assertEqual(worker.exitcode, 0)
            self.assertEqual(len(SavedHostsConfig(path).get_all()), 20)

    def test_stale_metadata_does_not_restore_removed_preferences(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = SavedHostsConfig(Path(tmp)/'hosts.json')
            c.save_host(SavedHost(ip='192.0.2.1', auto_connect=[AutoConnectDevice('1-1','0079','1804')]))
            stale = c.get('192.0.2.1')
            c.remove_auto_connect_device(stale.ip, '1-1')
            stale.hostname = 'new-hostname'
            c.save_host(stale)
            self.assertEqual(c.get(stale.ip).auto_connect, [])
            self.assertEqual(c.get(stale.ip).hostname, 'new-hostname')

    def test_stale_busid_refresh_does_not_restore_removed_device(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = SavedHostsConfig(Path(tmp)/'hosts.json')
            c.save_host(SavedHost(ip='192.0.2.1', auto_connect=[AutoConnectDevice('1-1','0079','1804')]))
            stale = c.get('192.0.2.1')
            c.remove_auto_connect_device(stale.ip, '1-1')
            stale.auto_connect[0].busid = '1-2'
            c.save_host(stale)
            self.assertEqual(c.get(stale.ip).auto_connect, [])

    def test_removed_host_is_not_restored_by_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = SavedHostsConfig(Path(tmp)/'hosts.json')
            c.save_host(SavedHost(ip='192.0.2.1', machine_id='host-id'))
            stale = c.get('192.0.2.1')
            c.remove_host(stale.ip)
            stale.ip = '192.0.2.2'
            self.assertFalse(c.save_host(stale))
            self.assertEqual(c.get_all(), [])

    def test_concurrent_rename_and_preferences_both_survive(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = SavedHostsConfig(Path(tmp)/'hosts.json')
            c.save_host(SavedHost(ip='192.0.2.1'))
            stale = c.get('192.0.2.1')
            c.rename_host(stale.ip, 'Custom name')
            stale.auto_connect.append(AutoConnectDevice('1-1','0079','1804'))
            c.save_host(stale)
            result = c.get(stale.ip)
            self.assertEqual(result.name, 'Custom name')
            self.assertEqual(len(result.auto_connect), 1)

    def test_learning_machine_id_keeps_preferences(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = SavedHostsConfig(Path(tmp)/'hosts.json')
            c.save_host(SavedHost(ip='192.0.2.1',name='Custom',auto_connect=[AutoConnectDevice('1-1','0079','1804')]))
            with patch.object(client_menu,'get_config',return_value=c):
                client_menu._maybe_save_host('192.0.2.1',remote=RemoteHost('192.0.2.1','pi',3240,1,machine_id='new-id'))
            self.assertEqual(c.get('192.0.2.1').name, 'Custom')
            self.assertEqual(len(c.get('192.0.2.1').auto_connect),1)

    def test_edit_removal_survives_final_save(self):
        with tempfile.TemporaryDirectory() as tmp:
            c=SavedHostsConfig(Path(tmp)/'hosts.json')
            c.save_host(SavedHost(ip='192.0.2.1',auto_connect=[AutoConnectDevice('1-1','0079','1804')]))
            with patch.object(client_menu,'get_config',return_value=c), \
                 patch.object(client_menu,'prompt_choice',side_effect=[0,0]), \
                 patch.object(client_menu,'prompt',side_effect=['Custom','','3240']), \
                 patch.object(client_menu,'prompt_yes_no',return_value=True), \
                 patch.object(client_menu,'clear_screen'),patch.object(client_menu,'pause'):
                client_menu._edit_saved_host(c.get_all())
            self.assertEqual(c.get('192.0.2.1').auto_connect,[])

    def test_corrupt_config_is_not_replaced(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name, store, write in [('hosts',SavedHostsConfig,lambda c:c.save_host(SavedHost(ip='x'))),
                                       ('state',DaemonState,lambda c:c.set_bindings([]))]:
                path=Path(tmp)/(name+'.json')
                path.write_text('[]')
                with self.assertRaises(OSError): write(store(path))
                self.assertEqual(path.read_text(),'[]')

    def test_state_transaction_preserves_cli_change_during_daemon_save(self):
        with tempfile.TemporaryDirectory() as tmp:
            state=DaemonState(Path(tmp)/'state.json')
            state.set_bindings([BoundDevice('1-1','0079','1804')])
            other=DaemonState(state._path)
            started=threading.Event()
            finish=threading.Event()
            def update(current):
                started.set()
                self.assertTrue(finish.wait(3))
                return current
            t=threading.Thread(target=lambda:state.update_bindings(update))
            t.start()
            self.assertTrue(started.wait(3))
            saved=[]
            writer=threading.Thread(target=lambda:saved.append(other.update_bindings(lambda current:current+[BoundDevice('1-2','0079','1804')])))
            writer.start()
            finish.set()
            t.join(3);writer.join(3)
            self.assertEqual(saved,[True])
            self.assertEqual([d.busid for d in state.get_bindings()],['1-1','1-2'])


class RecoveryTests(unittest.TestCase):
    def connection(self):
        return ActiveConnection('192.0.2.1','pi','1-1',0,'SNES',vendor_id='0079',product_id='1804')

    def test_reused_port_is_not_alive(self):
        wrong=usbip.AttachedPort(0,'9-9','192.0.2.9','0079','1804')
        with patch.object(usbip,'list_attached_ports',return_value=[wrong]):
            self.assertFalse(self.connection().is_alive())

    def test_removing_saved_intent_disables_daemon_reconnect(self):
        with tempfile.TemporaryDirectory() as tmp:
            c=SavedHostsConfig(Path(tmp)/'hosts.json')
            c.save_host(SavedHost(ip='192.0.2.1'))
            session=ClientSession();session._running=True
            conn=self.connection();conn._saved_intent=True;session._connections[0]=conn
            with patch('usb_portal.saved_hosts.get_config',return_value=c), \
                 patch.object(usbip,'list_attached_ports',return_value=[]), \
                 patch.object(session,'_attempt_reconnect') as reconnect:
                session._check_connections()
            self.assertFalse(conn.reconnect_enabled)
            reconnect.assert_not_called()

    def test_unrelated_connection_does_not_suppress_saved_host_attempt(self):
        session=ClientSession();session._running=True
        conn=self.connection();conn.busid='9-9';session._connections[0]=conn
        saved=SavedHost(ip=conn.host_ip,auto_connect=[AutoConnectDevice('1-1','0079','1804')])
        with patch('usb_portal.saved_hosts.get_config') as config, \
             patch.object(session._stop,'wait',side_effect=[False,True]), \
             patch.object(session,'_auto_connect_host') as connect:
            config.return_value.get_all.return_value=[saved]
            session._auto_connect_loop()
        connect.assert_called_once_with(saved)

    def test_disconnect_during_lookup_cancels_reconnect(self):
        session=ClientSession();session._running=True
        conn=self.connection();session._connections[0]=conn
        def lookup(connection):
            session.disconnect(0)
            return '1-1'
        with patch.object(session,'_resolve_remote_busid',side_effect=lookup), \
             patch.object(usbip,'list_attached_ports',return_value=[]), \
             patch.object(usbip,'attach') as attach:
            session._attempt_reconnect(conn)
        attach.assert_not_called()
        self.assertEqual(session.get_connections(),[])

    def test_disconnect_during_attach_cleans_up_new_kernel_port(self):
        session=ClientSession();session._running=True
        conn=self.connection();session._connections[0]=conn
        target=usbip.AttachedPort(4,'1-2','192.0.2.1','0079','1804')
        def attach(*args):
            self.assertIs(session._connections.get(0),conn)
            session.disconnect(0)
            return 4
        with patch.object(session,'_resolve_remote_busid',return_value='1-2'), \
             patch.object(session,'_attach_remote',side_effect=attach), \
             patch.object(usbip,'list_attached_ports',side_effect=[[],[target]]), \
             patch.object(usbip,'detach') as detach:
            session._attempt_reconnect(conn)
        detach.assert_called_once_with(4,expected=target)
        self.assertEqual(session.get_connections(),[])

    def test_disconnect_does_not_detach_reused_port(self):
        session=ClientSession();session._connections[0]=self.connection()
        with patch.object(usbip,'list_attached_ports',return_value=[usbip.AttachedPort(0,'9-9','192.0.2.9','0079','1804')]), \
             patch.object(usbip,'detach') as detach:
            session.disconnect(0)
        detach.assert_not_called()

    def test_detach_rechecks_identity_after_taking_process_lock(self):
        original=usbip.AttachedPort(0,'1-1','192.0.2.1','0079','1804')
        reused=usbip.AttachedPort(0,'9-9','192.0.2.9','0079','1804')
        with patch.object(usbip,'file_lock',return_value=nullcontext()), \
             patch.object(usbip,'list_attached_ports',return_value=[reused]), \
             patch.object(usbip,'_run') as run:
            usbip.detach(0,expected=original)
        run.assert_not_called()

    def test_already_satisfied_intent_does_not_attach_extra_identical_device(self):
        saved=[AutoConnectDevice('1-1','0079','1804')]
        devices=[usbip.UsbipDevice(b,'0079','1804','','') for b in ['1-1','1-2']]
        self.assertEqual(_resolve_auto_connect_devices(saved,devices,{'1-1'}),[])

    def test_two_identical_connections_survive_adoption_without_collapsing_intent(self):
        with tempfile.TemporaryDirectory() as tmp:
            c=SavedHostsConfig(Path(tmp)/'hosts.json')
            c.save_host(SavedHost(ip='192.0.2.1',auto_connect=[AutoConnectDevice(b,'0079','1804') for b in ['1-1','1-2']]))
            attached=[usbip.AttachedPort(i,b,'192.0.2.1','0079','1804') for i,b in enumerate(['1-1','1-2'])]
            with patch('usb_portal.saved_hosts.get_config',return_value=c),patch.object(usbip,'list_attached_ports',return_value=attached):
                session=ClientSession();session._discover_existing_connections()
            self.assertEqual([d.busid for d in c.get('192.0.2.1').auto_connect],['1-1','1-2'])
            self.assertEqual(len(session.get_connections()),2)

    def test_attach_adopts_existing_kernel_connection_under_process_lock(self):
        with patch.object(usbip,'file_lock',return_value=nullcontext()), \
             patch.object(usbip,'list_attached_ports',return_value=[usbip.AttachedPort(4,'1-1','192.0.2.1','0079','1804',remote_port=4321)]), \
             patch.object(usbip,'_attach') as attach:
            self.assertEqual(usbip.attach('192.0.2.1','1-1',4321),4)
        attach.assert_not_called()

    def test_port_parser_keeps_endpoint_and_rejects_unused_ports(self):
        output='''Port 00: <Port in Use> at Low Speed(1.5Mbps)
  DragonRise Inc. : SNES (0079:1804)
  11-1 -> usbip://[fd00::1]:4321/1-1.3.1
       -> remote bus/dev 001/004
Port 01: <Port Available>
'''
        ports=usbip._parse_port_list(output)
        self.assertEqual(len(ports),1)
        self.assertEqual((ports[0].remote_host,ports[0].remote_port),('fd00::1',4321))

    def test_shutdown_does_not_restart_usbipd(self):
        daemon=GamepadDaemon();daemon._running=False;daemon._stop.set()
        with patch.object(daemon,'_start_usbipd') as start:
            daemon._health_loop()
        start.assert_not_called()


class DiscoveryHardeningTests(unittest.TestCase):
    def test_listener_continues_after_malformed_json_object(self):
        scanner=DiscoveryScanner();scanner._running=True
        with patch.object(scanner,'_bind_socket') as bind:
            sock=bind.return_value
            good=json.dumps({'app':'usb-portal','type':'announce','hostname':'pi'}).encode()
            sock.recvfrom.side_effect=[(b'[]',('192.0.2.1',3241)),(good,('192.0.2.1',3241)),OSError('closed')]
            scanner._listen()
        self.assertEqual(len(scanner.hosts),1)
        self.assertFalse(scanner._running)
        sock.close.assert_called_once()

    def test_malformed_packets_are_ignored_and_good_packet_still_works(self):
        scanner=DiscoveryScanner()
        for value in [[],None,True,1,'string',{'app':'usb-portal','type':'announce','port':-1},
                      {'app':'usb-portal','type':'announce','hostname':[]},
                      {'app':'usb-portal','type':'announce','devices':'2'}]:
            scanner._handle_packet(json.dumps(value).encode(),'192.0.2.1')
        scanner._handle_packet(b'\xff','192.0.2.1')
        self.assertEqual(scanner.hosts,[])
        scanner._handle_packet(json.dumps({'app':'usb-portal','type':'announce','hostname':'pi'}).encode(),'192.0.2.1')
        self.assertEqual(len(scanner.hosts),1)

    def test_raw_machine_id_is_transformed_stably(self):
        raw='0123456789abcdef0123456789abcdef'
        result=private_machine_id(raw)
        self.assertNotIn(raw,result)
        self.assertEqual(result,private_machine_id(result))
        with patch('usb_portal.discovery.Path.read_text',return_value=raw):
            from usb_portal.discovery import DiscoveryAdvertiser
            self.assertEqual(json.loads(DiscoveryAdvertiser('pi',3240,lambda:2)._make_announce())['machine_id'],result)

    def test_public_and_ipv6_scans_are_rejected_before_opening_sockets(self):
        for network in ['0.0.0.0/0','8.8.8.0/24','fd00::/64']:
            with self.assertRaises(ValueError):scan_cidr(network)


@unittest.skipUnless(shutil.which('cc'), 'C compiler required')
class InstallerTests(unittest.TestCase):
    def install_script(self,tmp,fail_compiler,umask=None):
        source=Path(__file__).resolve().parents[1]
        functions=(source/'install.sh').read_text().split('\nACTION=')[0]
        install=Path(tmp)/'installed';install.mkdir();(install/'marker').write_text('old install')
        binary=Path(tmp)/'bin';binary.mkdir()
        script=Path(tmp)/'installer-test.sh'
        setup=f'''\nSRC_DIR={shlex.quote(str(source))}
INSTALL_LIB={shlex.quote(str(install))}
INSTALL_BIN={shlex.quote(str(binary))}
migrate_old_install() {{ :; }}
systemctl() {{ return 1; }}
'''
        if umask is not None:
            setup+=f'umask {umask}\n'
        if fail_compiler:
            setup+='cc() { return 1; }\nif install_files; then exit 90; fi\n'
        else:
            setup+='install_files\n'
        script.write_text(functions+setup)
        result=subprocess.run(['bash',str(script)],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        return install,binary

    def test_failed_compile_preserves_installed_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            install,_=self.install_script(tmp,True)
            self.assertEqual((install/'marker').read_text(),'old install')

    def test_installer_builds_policy_before_replacing_installation(self):
        with tempfile.TemporaryDirectory() as tmp:
            install,binary=self.install_script(tmp,False)
            self.assertFalse((install/'marker').exists())
            self.assertTrue((install/'usb_portal/native/socket_policy.so').is_file())
            self.assertTrue((binary/'usb-portal-daemon').is_file())

    def test_restrictive_caller_umask_keeps_installed_code_readable(self):
        with tempfile.TemporaryDirectory() as tmp:
            install,binary=self.install_script(tmp,False,umask='077')
            self.assertEqual(install.stat().st_mode & 0o777,0o755)
            for path in (install/'usb_portal').rglob('*'):
                if path.is_dir():
                    self.assertEqual(path.stat().st_mode & 0o777,0o755,str(path))
                elif path.is_file():
                    expected=0o755 if path.suffix=='.so' else 0o644
                    self.assertEqual(path.stat().st_mode & 0o777,expected,str(path))
            for name in ('usb-portal','usb-portal-daemon'):
                self.assertEqual((binary/name).stat().st_mode & 0o777,0o755)

    def test_launcher_uses_installed_package_when_cwd_contains_another_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            _,binary=self.install_script(tmp,False)
            shadow=Path(tmp)/'shadow';package=shadow/'usb_portal';package.mkdir(parents=True)
            (package/'__init__.py').write_text('raise RuntimeError("Wrong package loaded from current directory")')
            result=subprocess.run([str(binary/'usb-portal'),'--version'],cwd=shadow,capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertIn('1.3.0',result.stdout)


@unittest.skipUnless(shutil.which('cc'), 'C compiler required')
class SocketPolicyTests(unittest.TestCase):
    def test_socket_options_on_outbound_and_accepted_connections(self):
        try:
            probe=socket.socket()
            probe.close()
        except PermissionError:
            self.skipTest('Network sockets are disabled by this execution sandbox')
        with tempfile.TemporaryDirectory() as tmp:
            library=Path(tmp)/'policy.so'
            source=Path(__file__).resolve().parents[1]/'usb_portal/native/socket_policy.c'
            subprocess.run(['cc','-shared','-fPIC','-O2','-Wall','-Wextra','-Werror','-o',str(library),str(source),'-ldl'],check=True)
            script='''import json,socket
s=socket.socket();s.bind(('127.0.0.1',0));s.listen()
c=socket.create_connection(s.getsockname());a,_=s.accept()
keys=[socket.TCP_NODELAY,socket.TCP_KEEPIDLE,socket.TCP_KEEPINTVL,socket.TCP_KEEPCNT,socket.TCP_USER_TIMEOUT]
print(json.dumps([[x.getsockopt(socket.IPPROTO_TCP,k) for k in keys]+[x.getsockopt(socket.SOL_SOCKET,socket.SO_KEEPALIVE)] for x in [c,a]]))
'''
            result=subprocess.run([sys.executable,'-c',script],env={**os.environ,'LD_PRELOAD':str(library)},check=True,capture_output=True,text=True)
            self.assertEqual(json.loads(result.stdout),[[1,5,3,3,15000,1]]*2)
