"""Size units, measurement limits, metadata fallbacks and theme preferences."""
import dataclasses
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pocket_uninstaller as p

class SizeTests(unittest.TestCase):
    def test_si_iec_and_unknown_are_distinct(self):
        for text, expected in [('1.5 MiB',1572864),('1.5 MB',1500000),('0 B',0),('123',123),('2 GiB',2147483648),('N/A',None),('-1',None),('',None),('10 frogs',None)]:
            with self.subTest(text=text): self.assertEqual(p.parse_size(text),expected)
        self.assertEqual(p.parse_size('2','KiB'),2048)
        self.assertEqual(p.format_size(None),'Unknown')
        self.assertEqual(p.format_size(0),'0 B')

    def test_unknown_last_in_both_numeric_sorts_and_excluded_from_total(self):
        apps=[p.App('APT',str(i),str(i),size_bytes=n) for i,n in enumerate([None,1024,0,1024**3])]
        self.assertEqual([a.size_bytes for a in p.sort_apps(apps,'largest')],[1024**3,1024,0,None])
        self.assertEqual([a.size_bytes for a in p.sort_apps(apps,'smallest')],[0,1024,1024**3,None])
        self.assertEqual(p.size_total(apps),(1024**3+1024,1))
        self.assertEqual(p.total_label(apps[:1])[0],'Unknown')
        self.assertEqual(p.total_label([])[0],'0 B')

    def test_apt_kib_and_unknown(self):
        apps=p.parse_apt('foo\t1\tinstalled\tno\t1025\tFoo\nbar\t2\tinstalled\tno\t\tBar\n')
        self.assertEqual(apps[0].size_bytes,1025*1024)
        self.assertEqual(apps[0].size_kind,'reported')
        self.assertIsNone(apps[1].size_bytes)

    def test_pacman_multiline_records(self):
        apps=p.parse_pacman('Name            : foo\nVersion         : 1.0\nDescription     : Foo\nInstalled Size  : 2.50 MiB\n\nName            : bar\nVersion         : 2\nInstalled Size  : 0.00 B\n')
        self.assertEqual([a.size_bytes for a in apps],[2621440,0])

    def test_rpm_large_size(self):
        with patch.object(p,'read_command',return_value='foo.x86_64\t1.0\t5368709120\tFoo\n'), patch.object(p,'tool',return_value='rpm'):
            apps=p.scan_rpm('DNF')
        self.assertEqual(apps[0].size_bytes,5*1024**3)

    def test_flatpak_si_metadata_preserves_scope(self):
        app=p.parse_flatpak('org.test.App/x86_64/stable\tTest\t1\tflathub\t12.5 MB\n','user')[0]
        self.assertEqual(app.size_bytes,12500000)
        self.assertEqual(app.scope,'user')
        self.assertIn('shared runtimes',app.size_note)

    def test_measure_hardlinks_once_and_never_follow_symlinks(self):
        with tempfile.TemporaryDirectory() as temp:
            parent=Path(temp); root=parent/'app'; root.mkdir()
            data=root/'data'; data.write_bytes(b'x'*8192)
            os.link(data,root/'duplicate')
            outside=parent/'outside'; outside.write_bytes(b'x'*32768)
            link=root/'link'; link.symlink_to(outside)
            expected=sum(x.lstat().st_blocks*512 for x in [root,data,link])
            self.assertEqual(p.allocated_size(root)[0],expected)
            self.assertIsNone(p.allocated_size(link)[0])

    def test_sparse_file_uses_allocation_and_limits_discard_partial(self):
        with tempfile.TemporaryDirectory() as temp:
            file=Path(temp)/'sparse'
            with file.open('wb') as f:
                f.seek(8*1024**2); f.write(b'x')
            self.assertEqual(p.allocated_size(file)[0],file.stat().st_blocks*512)
            self.assertIsNone(p.allocated_size(temp,max_entries=1)[0])
            self.assertIsNone(p.allocated_size(temp,timeout=-1)[0])
            self.assertIsNone(p.allocated_size(Path(temp)/'absent')[0])

    def test_pipx_metadata_survives_size_command_failure(self):
        output=json.dumps({'venvs':{'ruff':{'metadata':{'main_package':{'package_version':'1'}}}}})
        with patch.object(p,'read_command',side_effect=[output,RuntimeError('unsupported')]), patch.object(p,'tool',return_value='pipx'):
            apps=p.scan_pipx()
        self.assertEqual(apps[0].ident,'ruff')
        self.assertIsNone(apps[0].size_bytes)
        self.assertIn('unsupported',apps[0].size_note)

    def test_brew_only_measures_formula_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            cellar=Path(temp); (cellar/'wget').mkdir(); (cellar/'wget'/'data').write_bytes(b'x'*9000)
            with patch.object(p,'read_command',side_effect=['wget 1.0 1.1\n',temp]),patch.object(p,'tool',return_value='brew'):
                app=p.scan_brew()[0]
            self.assertEqual(app.size_bytes,p.allocated_size(cellar/'wget')[0])
            self.assertIn('installed versions',app.size_note)

    def test_snap_current_revision_file(self):
        import subprocess
        result=subprocess.CompletedProcess([],0,'Name Version Rev Tracking Publisher Notes\nfoo 1 42 latest/stable test -\n','')
        with patch.object(p.subprocess,'run',return_value=result),patch.object(p,'tool',return_value='snap'),patch.object(p,'measured_app',side_effect=lambda a,path,note: (a,path,note)):
            app,path,note=p.scan_snap()[0]
        self.assertEqual(path,Path('/var/lib/snapd/snaps/foo_42.snap'))
        self.assertIn('older revisions',note)

    def test_theme_roundtrip_and_invalid_preferences(self):
        with tempfile.TemporaryDirectory() as temp,patch.object(p,'STORE',Path(temp)/'store'):
            self.assertEqual(p.load_theme(),'light')
            p.save_theme('dark'); self.assertEqual(p.load_theme(),'dark')
            p.save_theme('light'); self.assertEqual(p.load_theme(),'light')
            (p.STORE/'preferences.json').write_text('{broken')
            self.assertEqual(p.load_theme(),'light')
            with self.assertRaises(ValueError): p.save_theme('invalid')

    def test_accent_and_sources_persist_without_touching_theme(self):
        with tempfile.TemporaryDirectory() as temp,patch.object(p,'STORE',Path(temp)/'store'):
            defaults=p.load_preferences()
            self.assertEqual(defaults,{'theme':'light','accent':'green','disabled_sources':[]})
            p.save_accent('blue')
            self.assertEqual(p.load_preferences()['theme'],'light')
            p.save_theme('dark')
            self.assertEqual(p.load_preferences(),{'theme':'dark','accent':'blue','disabled_sources':[]})
            p.save_disabled_sources(['Snap','APT','Snap'])
            self.assertEqual(p.load_preferences()['disabled_sources'],['APT','Snap'])
            self.assertEqual(p.load_preferences()['accent'],'blue')
            with self.assertRaises(ValueError): p.save_accent('orange')
            with self.assertRaises(ValueError): p.save_disabled_sources(['Nix'])

    def test_invalid_preference_values_fall_back_individually(self):
        with tempfile.TemporaryDirectory() as temp,patch.object(p,'STORE',Path(temp)/'store'):
            p.private_store()
            (p.STORE/'preferences.json').write_text(json.dumps(
                {'theme':'neon','accent':'purple','disabled_sources':['Snap','Nix',7]}))
            self.assertEqual(p.load_preferences(),{'theme':'light','accent':'green','disabled_sources':['Snap']})
            (p.STORE/'preferences.json').write_text(json.dumps(['not','a','dict']))
            self.assertEqual(p.load_preferences()['accent'],'green')

    def test_accents_are_distinct_with_readable_text(self):
        self.assertEqual(set(p.ACCENTS),{'green','red','yellow','pink','blue','cyan'})
        self.assertEqual(len({v['side'] for v in p.ACCENTS.values()}),6)
        self.assertEqual(len({v['accent'] for v in p.ACCENTS.values()}),6)
        def luminance(color):
            r,g,b=(int(color[i:i+2],16)/255 for i in (1,3,5))
            f=lambda c:c/12.92 if c<=.03928 else ((c+.055)/1.055)**2.4
            return .2126*f(r)+.7152*f(g)+.0722*f(b)
        def ratio(a,b):
            x,y=sorted((luminance(a),luminance(b)),reverse=True)
            return (x+.05)/(y+.05)
        for name,palette in p.ACCENTS.items():
            with self.subTest(accent=name):
                self.assertGreaterEqual(ratio(palette['side_text'],palette['side']),4.5)
                self.assertGreaterEqual(ratio(palette['ink'],palette['accent']),4.5)
                self.assertGreaterEqual(ratio(palette['side_muted'],palette['side']),3.0)

    def test_disabled_sources_are_not_scanned(self):
        calls=[]
        with patch.object(p,'tool',side_effect=lambda n,*a:'/bin/'+n), \
             patch.object(p,'scan_pipx',side_effect=lambda: calls.append('pipx') or [p.App('pipx','x','X','','user')]), \
             patch.object(p,'scan_apt',side_effect=lambda: calls.append('apt') or [p.App('APT','a','A')]), \
             patch.object(p,'scan_snap',side_effect=lambda: calls.append('snap') or []), \
             patch.object(p,'scan_flatpak',return_value=[]), patch.object(p,'scan_brew',return_value=[]), \
             patch.object(p,'scan_appimages',return_value=[]), patch.object(p,'scan_rpm',return_value=[]), \
             patch.object(p,'flatpak_installations',return_value=[]):
            apps,_=p.scan_all(enabled={'APT','pipx'})
        self.assertEqual(sorted(calls),['apt','pipx'])
        self.assertEqual({a.manager for a in apps},{'APT','pipx'})

    def test_scan_all_defaults_to_every_source(self):
        with patch.object(p,'tool',return_value=None), patch.object(p,'scan_appimages',return_value=[p.App('AppImage','/home/u/a.AppImage','a')]):
            apps,_=p.scan_all()
        self.assertEqual([a.manager for a in apps],['AppImage'])

if __name__=='__main__': unittest.main()
