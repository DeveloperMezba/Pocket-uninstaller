"""Read-only metadata and truthful identity/category checks."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pocket_uninstaller as p

class InfoTests(unittest.TestCase):
    def test_apt_description_and_maintainer_are_distinct(self):
        data='Web browser\n Browse the web.\n .\n Supports extensions.\x1fDebian Browser Team <team@example.org>\x1fhttps://example.org\x1fweb'
        with patch.object(p,'read_command',return_value=data) as command:
            info=p.get_package_info(p.App('APT','browser:amd64','Browser'))
        self.assertEqual(info.publisher_role,'Package maintainer')
        self.assertEqual(info.category,'Web browser (inferred)')
        self.assertIn('\n\nSupports',info.description)
        self.assertIn('may differ',info.notes)
        self.assertEqual(command.call_args.args[0][-1],'browser:amd64')

    def test_failures_keep_known_description_and_unknown_publisher(self):
        with patch.object(p,'read_command',side_effect=TimeoutError('timed out')):
            info=p.get_package_info(p.App('APT','foo','foo',note='Video player'))
        self.assertEqual(info.description,'Video player')
        self.assertEqual(info.publisher,'Not provided')
        self.assertIn('timed out',info.notes)

    def test_category_does_not_use_package_name_or_brand(self):
        self.assertEqual(p.make_info('Something useful').category,'Unknown')
        self.assertEqual(p.make_info('Shared library for web browsers',section='libs').category,'Library / dependency (inferred)')
        self.assertEqual(p.make_info('Documentation for a graphics driver').category,'Documentation (inferred)')
        self.assertEqual(p.make_info('NVIDIA display driver').category,'Hardware / graphics driver (inferred)')
        self.assertEqual(p.make_info('NVIDIA display driver').publisher,'Not provided')
        self.assertEqual(p.make_info('Wireless device firmware').category,'Device firmware (inferred)')

    def test_declared_category_has_explicit_basis(self):
        info=p.make_info('Browse',categories=['Network','WebBrowser'])
        self.assertEqual(info.category,'Web browser')
        self.assertIn('Declared',info.category_basis)

    def test_pacman_packager_and_continuation(self):
        data='Name : test\nDescription : A text editor\n              with plugins\nPackager : Packager Name\nURL : https://example.org\nGroups : None\n'
        with patch.object(p,'read_command',return_value=data):
            info=p.get_package_info(p.App('Pacman','test','test'))
        self.assertEqual(info.publisher_role,'Packager')
        self.assertIn('with plugins',info.description)

    def test_rpm_vendor_fallback_is_labelled_packager(self):
        with patch.object(p,'read_command',return_value='Video player\x1f(none)\x1fDistro Team\x1fhttps://example.org\x1fMultimedia'):
            info=p.get_package_info(p.App('DNF','test.x86_64','Test'))
        self.assertEqual(info.publisher_role,'Packager')
        self.assertEqual(info.publisher,'Distro Team')

    def test_flatpak_requires_matching_component_and_scope(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'files/share/metainfo'; path.mkdir(parents=True)
            (path/'other.xml').write_text('<component><id>org.other.App</id><developer_name>Wrong</developer_name></component>')
            (path/'test.xml').write_text('<component><id>org.test.App</id><summary>Web browser</summary><developer><name>Example Team</name></developer><description><p>Browse safely.</p></description><url type="homepage">https://example.org</url><categories><category>WebBrowser</category></categories></component>')
            with patch.object(p,'read_command',return_value=temp) as command:
                info=p.get_package_info(p.App('Flatpak','app/org.test.App/x86_64/stable','Test',scope='installation:extra'))
        self.assertEqual(info.publisher,'Example Team')
        self.assertIn('--installation=extra',command.call_args.args[0])
        self.assertEqual(info.category,'Web browser')

    def test_flatpak_rejects_entity_documents(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'files/share/metainfo'; path.mkdir(parents=True)
            (path/'test.xml').write_text('<!DOCTYPE component [<!ENTITY name "Forged">]><component><id>org.test.App</id><developer_name>&name;</developer_name></component>')
            with patch.object(p,'read_command',return_value=temp):
                info=p.get_package_info(p.App('Flatpak','app/org.test.App/x86_64/stable','Test'))
        self.assertEqual(info.publisher,'Not provided')

    def test_pipx_matches_main_distribution_not_dependency(self):
        with tempfile.TemporaryDirectory() as temp:
            site=Path(temp)/'foo/lib/python3.12/site-packages'; site.mkdir(parents=True)
            for name,author in [('dependency','Wrong'),('real-package','Actual Author')]:
                d=site/(name+'.dist-info'); d.mkdir()
                (d/'METADATA').write_text(f'Name: {name}\nSummary: Code editor\nAuthor: {author}\nProject-URL: Homepage, https://example.org\n\n')
            data={'venvs':{'foo':{'metadata':{'main_package':{'package':'real_package'}}}}}
            with patch.object(p,'read_command',side_effect=[json.dumps(data),temp]):
                info=p.get_package_info(p.App('pipx','foo','Foo'))
        self.assertEqual(info.publisher,'Actual Author')
        self.assertEqual(info.homepage,'https://example.org')

    def test_homebrew_reads_formula_without_executing_it(self):
        with tempfile.TemporaryDirectory() as temp:
            d=Path(temp)/'foo/1.0/.brew'; d.mkdir(parents=True)
            (d/'foo.rb').write_text('class Foo\n  desc "A text editor"\n  homepage "https://example.org"\n  system "should never run"\nend\n')
            with patch.object(p,'read_command',return_value=temp) as command:
                info=p.get_package_info(p.App('Homebrew','foo','Foo'))
        self.assertEqual(info.description,'A text editor')
        self.assertEqual(command.call_count,1)
        self.assertEqual(info.publisher,'Not provided')

    def test_snap_uses_local_read_only_installed_endpoint(self):
        import http.client
        response=Mock(status=200)
        response.read.return_value=json.dumps({'result':{'summary':'A media player','publisher':{'display-name':'Team'},'type':'app'}}).encode()
        with patch.object(http.client.HTTPConnection,'request') as request,patch.object(http.client.HTTPConnection,'getresponse',return_value=response):
            info=p.get_package_info(p.App('Snap','player','Player'))
        request.assert_called_once_with('GET','/v2/snaps/player')
        self.assertEqual(info.publisher,'Team')
        self.assertEqual(info.publisher_role,'Snap publisher')

    def test_appimage_desktop_exact_exec_match(self):
        with tempfile.TemporaryDirectory() as temp:
            d=Path(temp)/'applications'; d.mkdir()
            (d/'wrong.desktop').write_text('[Desktop Entry]\nExec=/other/Test.AppImage\nComment=Wrong\n')
            (d/'right.desktop').write_text('[Desktop Entry]\nExec="/home/user/My Test.AppImage" %U\nComment=A drawing tool\nCategories=Graphics;\n')
            with patch.object(p,'xdg_root',return_value=Path(temp)),patch.object(p,'read_command') as command:
                info=p.get_package_info(p.App('AppImage','/home/user/My Test.AppImage','Test'))
            command.assert_not_called()
        self.assertEqual(info.description,'A drawing tool')
        self.assertEqual(info.category,'Graphics software')

    def test_invalid_ident_never_reaches_command(self):
        with patch.object(p,'read_command') as command:
            info=p.get_package_info(p.App('APT','--help','bad'))
        command.assert_not_called()
        self.assertIn('Invalid',info.notes)

    def test_metadata_limits_and_unsafe_urls(self):
        self.assertEqual(p.make_info('hello\x00there',homepage='javascript:alert(1)').homepage,'')
        self.assertEqual(p.make_info('hello\x00there').description,'hellothere')
        with tempfile.TemporaryDirectory() as temp:
            f=Path(temp)/'metadata'; f.write_text('a'*100)
            with self.assertRaises(ValueError): p.read_info_file(f,10)

if __name__=='__main__': unittest.main()
