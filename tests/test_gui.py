"""Real Tk event tests. Set DISPLAY to an X server; skipped on headless systems."""
import os
import sys
import time
from pathlib import Path
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pocket_uninstaller as p

@unittest.skipUnless(os.environ.get('DISPLAY'), 'needs a display server')
class KeyboardTests(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        self.root = tk.Tk(className='PocketUninstaller')
        apps=[p.App('APT',f'app-{i:03d}',f'Application {i:03d}',version='1.0',size_bytes=(i+1)*1024**2,size_kind='reported') for i in range(100)]
        apps.append(p.App('APT','bash','Bash',protected=True))
        self.mock=patch.object(p,'scan_all',return_value=(apps,[]))
        self.mock.start()
        self.info_mock=patch.object(p,'get_package_info',side_effect=lambda app:p.make_info('Description of '+app.ident,'Example Team','Developer',source='Fixture'))
        self.info_call=self.info_mock.start()
        self.ui=p.PocketUI(self.root)
        end=time.monotonic()+5
        while self.ui.busy and time.monotonic()<end:
            self.root.update()
            time.sleep(.02)
        self.assertFalse(self.ui.busy)
        self.root.focus_force()
        self.ui.tree.focus_set()
        self.root.update()

    def test_size_totals_track_search_and_hidden_selection(self):
        self.key('<space>')
        self.assertEqual(self.ui.selected_space.get(),'1.0 MiB')
        self.ui.search.set('Application 099')
        self.root.update()
        self.assertEqual(self.ui.view_space.get(),'100.0 MiB')
        self.assertEqual(self.ui.selected_space.get(),'1.0 MiB')
        self.ui.select_visible()
        self.assertEqual(self.ui.selected_space.get(),'101.0 MiB')

    def test_size_sort_keeps_focus_and_selection(self):
        self.key('<space>')
        focused=self.ui.visible[self.ui.tree.focus()].key
        self.ui.sort_size()
        self.root.update()
        self.assertEqual(next(iter(self.ui.visible.values())).ident,'app-099')
        self.assertEqual(self.ui.visible[self.ui.tree.focus()].key,focused)
        self.assertEqual(len(self.ui.chosen),1)
        self.assertEqual(list(self.ui.visible.values())[-1].ident,'bash')
        self.ui.sort_size()
        self.assertEqual(next(iter(self.ui.visible.values())).ident,'app-000')

    def test_theme_roundtrip_preserves_selection_and_readability(self):
        self.ui.apply_theme('light',persist=False)
        original=self.ui.entry.cget('background')
        self.key('<space>')
        with patch.object(p,'save_theme') as save:
            self.ui.apply_theme('dark')
            save.assert_called_once_with('dark')
        self.root.update()
        self.assertLess(self._luminance(self.ui.entry.cget('background')),0.15,
                        'search field did not become a dark surface')
        self.assertEqual(self.ui.entry.cget('foreground'),'#e7f2e3')
        self.assertEqual(len(self.ui.chosen),1)
        self.ui.set_source('Selected')
        self.root.update()
        self.assertEqual(len(self.ui.visible),1)
        self.ui.apply_theme('light',persist=False)
        self.root.update()
        self.assertEqual(self.ui.entry.cget('background'),original)
        self.assertEqual(len(self.ui.chosen),1)

    def test_late_dialog_controls_follow_dark_mode(self):
        self.ui.apply_theme('dark',persist=False)
        window=self.ui.text_window('Test','Size details')
        frame=self.ui.tk.Frame(window,bg=self.ui.bg)
        frame.pack()
        self.ui.button(frame,'Close',window.destroy).pack()
        self.root.update()
        self.assertEqual(frame.cget('background'),'#101b18')
        self.ui.apply_theme('light',persist=False)
        self.assertEqual(frame.cget('background'),self.ui.bg)
        window.destroy()

    def wait_info(self):
        end=time.monotonic()+3
        while self.ui.current_info is None and time.monotonic()<end:
            self.root.update()
            time.sleep(.02)
        self.assertIsNotNone(self.ui.current_info)

    def test_package_info_follows_keyboard_without_selecting(self):
        self.key('<Down>')
        self.wait_info()
        self.assertIn('app-001',self.ui.info_description.get())
        self.assertIn('Example Team',self.ui.info_identity.get())
        self.assertEqual(len(self.ui.chosen),0)

    def test_rapid_navigation_and_stale_results_show_current_app(self):
        old=(self.ui.info_epoch,self.ui.apps[0].key,self.ui.apps[0].version)
        for _ in range(8): self.key('<Down>')
        self.ui.info_results.put((old,p.make_info('Stale information','Wrong Team')))
        self.ui.poll_info()
        self.wait_info()
        self.assertIn('app-008',self.ui.info_description.get())
        self.assertNotIn('Wrong',self.ui.info_identity.get())
        self.assertLessEqual(self.info_call.call_count,2)

    def test_empty_filter_clears_package_info(self):
        self.wait_info()
        self.ui.search.set('nonexistent package')
        self.root.update()
        self.assertIsNone(self.ui.current_info)
        self.assertIn('Click an app',self.ui.info_description.get())
        self.assertEqual(self.ui.info_button.cget('state'),'disabled')

    def test_details_cached_and_refresh_invalidates_metadata(self):
        self.wait_info()
        self.key('<Down>'); self.wait_info()
        calls=self.info_call.call_count
        self.key('<Up>'); self.wait_info()
        self.assertEqual(self.info_call.call_count,calls)
        self.ui.scanned((self.ui.apps,[])); self.wait_info()
        self.assertGreater(self.info_call.call_count,calls)
        old_selection=dict(self.ui.chosen)
        self.ui.package_details()
        self.root.update()
        self.assertEqual(self.ui.chosen,old_selection)

    def tearDown(self):
        for item in self.root.tk.splitlist(self.root.tk.call('after','info')):
            self.root.after_cancel(item)
        self.root.destroy()
        self.mock.stop()
        self.info_mock.stop()

    def key(self,key):
        self.ui.tree.event_generate(key)
        self.root.update()

    def test_arrows_move_focus_and_space_preserves_multiple_choices(self):
        self.key('<space>')
        self.key('<Down>')
        self.assertEqual(self.ui.tree.focus(),'1')
        self.key('<space>')
        self.assertEqual(len(self.ui.chosen),2)
        self.key('<Up>')
        self.assertEqual(self.ui.tree.focus(),'0')
        self.assertEqual(len(self.ui.chosen),2)

    def test_end_page_and_home_scroll_into_view(self):
        self.key('<End>')
        self.assertEqual(self.ui.tree.focus(),'100')
        self.assertGreater(self.ui.tree.yview()[0],.5)
        self.key('<Prior>')
        self.assertLess(int(self.ui.tree.focus()),100)
        self.key('<Home>')
        self.assertEqual(self.ui.tree.focus(),'0')
        self.assertEqual(self.ui.tree.yview()[0],0)

    def test_shift_range_and_ctrl_a_exclude_protected(self):
        self.key('<Shift-Down>')
        self.assertEqual(len(self.ui.chosen),2)
        self.key('<Control-a>')
        self.assertEqual(len(self.ui.chosen),100)
        self.assertFalse(any(a.ident=='bash' for a in self.ui.chosen.values()))

    def test_selection_survives_search_source_change_and_refresh(self):
        self.key('<space>')
        self.ui.search.set('Application 099')
        self.root.update()
        self.assertEqual(len(self.ui.visible),1)
        self.assertEqual(len(self.ui.chosen),1)
        self.assertIn('1 selected outside',self.ui.hint.get())
        self.ui.set_source('Selected')
        self.ui.search.set('')
        self.root.update()
        self.assertEqual(len(self.ui.visible),1)
        self.ui.scanned((self.ui.apps,[]))
        self.assertEqual(len(self.ui.chosen),1)

    def test_action_buttons_stay_visible_at_small_window_size(self):
        self.root.geometry("900x620")
        self.root.update()
        for button in (self.ui.remove_button, self.ui.leftover_button):
            bottom=button.winfo_rooty()-self.root.winfo_rooty()+button.winfo_height()
            self.assertLessEqual(bottom,self.root.winfo_height())
            self.assertTrue(button.winfo_ismapped())

    def test_down_from_search_enters_list(self):
        self.ui.entry.focus_set()
        self.root.update()
        self.ui.entry.event_generate('<Down>')
        self.root.update()
        self.assertEqual(self.root.focus_get(),self.ui.tree)

    def test_mouse_click_toggles_without_modifier_and_esc_clears(self):
        ids=list(self.ui.visible)
        for iid in ids[:2]:
            x,y,w,h=self.ui.tree.bbox(iid)
            self.ui.tree.event_generate('<Button-1>',x=x+65,y=y+h//2)
            self.root.update()
        self.assertEqual(len(self.ui.chosen),2)
        self.key('<Escape>')
        self.assertEqual(len(self.ui.chosen),0)

    def test_settings_button_opens_window_with_both_pages(self):
        self.assertTrue(self.ui.settings_button.winfo_ismapped())
        self.assertFalse(hasattr(self.ui,'dark_button'),'top Dark button should be removed')
        self.assertFalse(hasattr(self.ui,'light_button'),'top Light button should be removed')
        self.ui.settings()
        self.root.update()
        window=self.ui.settings_window
        self.assertTrue(window.winfo_exists())
        self.assertEqual(set(self.ui.settings_tabs),{'appearance','packages'})
        self.assertEqual(set(self.ui.settings_theme_buttons),{'light','dark'})
        self.assertEqual(set(self.ui.settings_accent_buttons),{'green','red','yellow','pink','blue','cyan'})
        self.ui.show_settings_page('packages')
        self.root.update()
        self.assertEqual(len(self.ui.settings_source_vars),len(p.SOURCES))
        self.ui.close_settings()
        self.assertIsNone(self.ui.settings_window)

    def test_each_accent_updates_sidebar_and_primary_button(self):
        self.ui.settings()
        self.root.update()
        for name,palette in p.ACCENTS.items():
            with self.subTest(accent=name):
                self.ui.apply_accent(name,persist=False)
                self.root.update()
                self.assertEqual(self.ui.side.cget('background'),palette['side'])
                self.assertEqual(self.ui.remove_button.cget('background'),palette['accent'])
                self.assertEqual(self.ui.source_labels['All'].cget('foreground'),palette['side'])
                self.assertEqual(self.ui.settings_rail.cget('background'),palette['side'])
                self.assertEqual(self.ui.settings_accent_buttons[name].cget('background'),palette['accent'])

    def test_theme_and_accent_never_change_each_other(self):
        self.ui.apply_accent('blue',persist=False)
        for mode in ('dark','light'):
            with self.subTest(mode=mode):
                with patch.object(p,'save_theme'):
                    self.ui.apply_theme(mode)
                self.root.update()
                palette=p.ACCENTS['blue']
                self.assertEqual(self.ui.accent,'blue')
                self.assertEqual(self.ui.side.cget('background'),palette['side'])
                self.assertEqual(self.ui.remove_button.cget('background'),palette['accent'])
                self.assertEqual(self.ui.source_labels['All'].cget('background'),palette['accent'])
        with patch.object(p,'save_accent'):
            self.ui.apply_theme('dark',persist=False)
            self.ui.apply_accent('red')
        self.root.update()
        self.assertEqual(self.ui.theme_mode,'dark')
        self.assertLess(self._luminance(self.ui.entry.cget('background')),0.15,
                        'search field did not become a dark surface')

    def test_disabling_a_source_hides_it_and_prunes_selection(self):
        self.ui.set_source('APT')
        self.root.update()
        iid=next(iter(self.ui.visible))
        self.ui.toggle(iid)
        self.ui.paint()
        self.assertEqual(len(self.ui.chosen),1)
        self.ui.settings()
        self.ui.show_settings_page('packages')
        self.root.update()
        with patch.object(p,'save_disabled_sources'):
            self.ui.settings_source_vars['APT'].set(False)
            self.ui.apply_sources()
        end=time.monotonic()+5
        while self.ui.busy and time.monotonic()<end:
            self.root.update(); time.sleep(.02)
        self.assertIn('APT',self.ui.disabled_sources)
        self.assertNotIn('APT',{a.manager for a in self.ui.apps})
        self.assertNotIn('APT',self.ui.source_labels)
        self.assertEqual(self.ui.source,'All')
        self.assertEqual(self.ui.chosen,{})
        self.assertFalse(any(a.manager=='APT' for a in self.ui.visible.values()))
        self.assertIn('disabled',self.ui.status.get())
        with patch.object(p,'save_disabled_sources'):
            self.ui.settings_source_vars['APT'].set(True)
            self.ui.apply_sources()
        end=time.monotonic()+5
        while self.ui.busy and time.monotonic()<end:
            self.root.update(); time.sleep(.02)
        self.assertIn('APT',{a.manager for a in self.ui.apps})
        self.assertNotIn('APT',self.ui.disabled_sources)

    def _luminance(self, color):
        color=str(color).lstrip('#')
        r,g,b=(int(color[i:i+2],16)/255 for i in (0,2,4))
        channel=lambda c:c/12.92 if c<=.03928 else ((c+.055)/1.055)**2.4
        return .2126*channel(r)+.7152*channel(g)+.0722*channel(b)

    def _contrast(self, first, second):
        high,low=sorted((self._luminance(first),self._luminance(second)),reverse=True)
        return (high+.05)/(low+.05)

    def test_items_badge_is_readable_in_every_theme_and_accent(self):
        for accent in p.ACCENTS:
            for mode in ('light','dark'):
                with self.subTest(accent=accent,mode=mode):
                    self.ui.apply_accent(accent,persist=False)
                    self.ui.apply_theme(mode,persist=False)
                    self.root.update()
                    ratio=self._contrast(self.ui.total_label.cget('background'),
                                         self.ui.total_label.cget('foreground'))
                    self.assertGreaterEqual(ratio,4.5,
                        f'ITEMS badge unreadable: {self.ui.total_label.cget("foreground")} '
                        f'on {self.ui.total_label.cget("background")}')

    def test_package_source_checkboxes_are_visible_in_dark_mode(self):
        self.ui.apply_theme('dark',persist=False)
        self.ui.settings()
        self.ui.show_settings_page('packages')
        self.root.update()
        def walk(widget):
            yield widget
            for child in widget.winfo_children():
                yield from walk(child)
        checks=[w for w in walk(self.ui.settings_content) if w.winfo_class()=='Checkbutton']
        self.assertEqual(len(checks),len(p.SOURCES))
        for widget in checks:
            with self.subTest(source=widget.cget('text')):
                box=str(widget.cget('selectcolor'))
                self.assertNotIn(box.lower(),('#ffffff','white'),
                                 'checkbox box stayed light in dark mode')
                ratio=self._contrast(box,widget.cget('foreground'))
                self.assertGreaterEqual(ratio,4.5,'check mark unreadable inside its box')
                self.assertGreaterEqual(self._contrast(widget.cget('background'),widget.cget('foreground')),4.5,
                                        'checkbox label unreadable on its surface')

    def test_tree_checkbox_glyph_and_protected_marker_readable_in_dark(self):
        self.ui.apply_theme('dark',persist=False)
        self.ui.paint()
        self.root.update()
        style=self.ui.ttk.Style(self.root)
        row=style.lookup('Treeview','background')
        ink=style.lookup('Treeview','foreground')
        self.assertGreaterEqual(self._contrast(row,ink),4.5,'tree row text unreadable')
        first=self.ui.tree.get_children()[0]
        self.ui.toggle(first); self.ui.paint(); self.root.update()
        self.assertEqual(self.ui.tree.set(first,'check'),'☑')
        protected=[i for i in self.ui.tree.get_children() if self.ui.visible[i].ident=='bash'][0]
        self.assertEqual(self.ui.tree.set(protected,'check'),'−')
        self.assertGreaterEqual(self._contrast(self.ui.tree.tag_configure('protected','foreground'),row),3.0,
                                'protected marker too faint')
        self.ui.toggle(first)

    def test_accent_repaints_body_not_only_sidebar(self):
        """Changing accent must restyle the main pane, not just the sidebar."""
        self.ui.apply_theme('dark',persist=False)
        self.ui.apply_accent('green',persist=False)
        self.root.update()
        def walk(widget):
            yield widget
            for child in widget.winfo_children():
                yield from walk(child)
        def under(widget, ancestor):
            node=widget
            while node is not None:
                if node is ancestor: return True
                node=getattr(node,'master',None)
            return False
        body=[w for w in walk(self.root) if 'background' in w.keys() and not under(w,self.ui.side)]
        before={w:(str(w.cget('background')),str(w.cget('foreground')) if 'foreground' in w.keys() else '')
                for w in body}
        self.ui.apply_accent('red',persist=False)
        self.root.update()
        after={w:(str(w.cget('background')),str(w.cget('foreground')) if 'foreground' in w.keys() else '')
               for w in body if w.winfo_exists()}
        changed=[w for w in after if before.get(w)!=after[w]]
        self.assertGreaterEqual(len(changed),10,
            f'only {len(changed)} body widgets followed the accent; sidebar-only recolouring')
        # The accent must reach surfaces beyond its two obvious widgets: the
        # metrics cards, search field, info panel and selection bar are all
        # tinted, so a sidebar-only implementation cannot satisfy this.
        tinted=[w for w in body if w.winfo_exists() and w in self.ui.accent_tagged
                and 'background' in self.ui.accent_tagged[w]]
        self.assertGreaterEqual(len(tinted),12,
            f'only {len(tinted)} body widgets are accent-background owned')
        kinds={self.ui.accent_tagged[w]['background'] for w in tinted}
        self.assertIn('tint',kinds,'no body surface uses the accent tint')
        self.assertIn('tint_soft',kinds,'no secondary body surface uses the accent tint')

    def test_every_accent_theme_pair_keeps_text_readable(self):
        """No visible widget may fall below 3:1 in any accent/theme combination."""
        def walk(widget):
            yield widget
            for child in widget.winfo_children():
                yield from walk(child)
        bad=[]
        for accent in p.ACCENTS:
            for mode in ('light','dark'):
                self.ui.apply_accent(accent,persist=False)
                self.ui.apply_theme(mode,persist=False)
                self.root.update()
                for widget in walk(self.root):
                    if 'foreground' not in widget.keys() or 'background' not in widget.keys():
                        continue
                    if not widget.winfo_ismapped():
                        continue
                    fg, bg = str(widget.cget('foreground')), str(widget.cget('background'))
                    if not fg.startswith('#') or not bg.startswith('#'):
                        continue
                    ratio=self._contrast(fg,bg)
                    if ratio<3.0:
                        bad.append(f'{accent}/{mode} {widget.winfo_class()} '
                                   f'{fg} on {bg} = {ratio:.2f}:1')
        self.assertEqual(bad,[],'unreadable widget colours: '+'; '.join(bad[:6]))

    def test_settings_tab_highlight_moves_with_selected_page(self):
        self.ui.settings()
        self.ui.show_settings_page('appearance')
        self.root.update()
        active=self.ui.settings_tabs['appearance'].cget('background')
        inactive=self.ui.settings_tabs['packages'].cget('background')
        self.assertEqual(active,self.ui.lime,'selected page is not highlighted')
        self.assertNotEqual(active,inactive,'both tabs look identical')
        self.assertIn('●',self.ui.settings_tabs['appearance'].cget('text'))
        self.ui.show_settings_page('packages')
        self.root.update()
        self.assertEqual(self.ui.settings_tabs['packages'].cget('background'),self.ui.lime,
                         'highlight did not follow the newly selected page')
        self.assertNotEqual(self.ui.settings_tabs['appearance'].cget('background'),self.ui.lime,
                            'old page kept the highlight')
        self.assertNotIn('●',self.ui.settings_tabs['appearance'].cget('text'))

    def test_large_inventory_stays_responsive(self):
        """A repaint must not scale with the whole inventory.

        Counted rather than timed: wall-clock thresholds are too noisy to be a
        reliable gate, but the number of rows touched per keypress is exact.
        """
        apps=[p.App('APT',f'pkg-{i:04d}',f'Package {i:04d}',version='1.0',
                    size_bytes=(i+1)*4096,size_kind='reported') for i in range(1200)]
        self.ui.scanned((apps,[]))
        self.root.update()
        rows=len(self.ui.visible)
        self.assertGreater(rows,1000)
        calls={'n':0}
        original=self.ui.cache.is_protected
        def counted(app):
            calls['n']+=1
            return original(app)
        self.ui.cache.is_protected=counted
        try:
            calls['n']=0
            for _ in range(10):
                self.ui.navigate('Down')
                self.root.update_idletasks()
        finally:
            self.ui.cache.is_protected=original
        per_key=calls['n']/10
        self.assertLess(per_key,20,
            f'navigating inspected {per_key:.0f} of {rows} rows per keypress; '
            'the repaint is scanning the whole inventory')
        # And it still has to be genuinely fast.
        start=time.perf_counter()
        for _ in range(20):
            self.ui.navigate('Down'); self.root.update_idletasks()
        per_move=(time.perf_counter()-start)/20
        self.assertLess(per_move,0.050,f'navigate took {per_move*1000:.1f} ms per keypress')

    def test_row_glyphs_and_selection_render_for_all_rows(self):
        """Incremental painting must still draw every row, not just the focus."""
        self.ui.scanned((self.ui.apps,[]))
        self.root.update()
        for iid in list(self.ui.visible)[:20]:
            self.assertIn(self.ui.tree.set(iid,'check'),('☐','☑','−'),
                          'a row was left without a checkbox glyph')
        protected=[i for i,a in self.ui.visible.items() if a.ident=='bash']
        if protected:
            self.assertEqual(self.ui.tree.set(protected[0],'check'),'−')

    def test_search_filters_correctly_and_updates_all_rows(self):
        self.ui.search.set('Application 099')
        self.root.update()
        self.assertEqual(len(self.ui.visible),1)
        self.assertEqual(next(iter(self.ui.visible.values())).ident,'app-099')
        for iid in self.ui.visible:
            self.assertIn(self.ui.tree.set(iid,'check'),('☐','☑'))
        self.ui.search.set('')
        self.root.update()
        self.assertEqual(len(self.ui.visible),len([a for a in self.ui.apps]))

    def test_list_surface_follows_accent_in_both_themes(self):
        """The package list must use the accent tint, not a flat theme colour."""
        for mode in ('light','dark'):
            with self.subTest(mode=mode):
                self.ui.apply_theme(mode,persist=False)
                sampled=[]
                for accent in ('green','blue'):
                    self.ui.apply_accent(accent,persist=False)
                    self.root.update()
                    style=self.ui.ttk.Style(self.root)
                    sampled.append((style.lookup('Treeview','background'),self.ui.tint))
                self.assertNotEqual(sampled[0][0],sampled[1][0],
                                    f'list background ignored the accent in {mode} mode')
                for background,tint in sampled:
                    self.assertEqual(background.lower(),tint.lower(),
                                     f'{mode} list surface is not the accent tint')

    def test_checked_rows_keep_highlight_and_others_do_not(self):
        ids=list(self.ui.visible)[:3]
        self.ui.toggle(ids[0]); self.ui.paint(); self.root.update()
        self.assertEqual(self.ui.tree.selection(),(ids[0],))
        self.ui.toggle(ids[2]); self.ui.paint(); self.root.update()
        self.assertEqual(set(self.ui.tree.selection()),{ids[0],ids[2]})
        # Unchecked rows must not carry a background tag of their own.
        for iid in ids[1:2]:
            self.assertNotIn('odd',self.ui.tree.item(iid,'tags'))
            self.assertNotIn('active',self.ui.tree.item(iid,'tags'))

    def test_settings_controls_stay_visible_at_minimum_size(self):
        self.ui.settings()
        window=self.ui.settings_window
        window.geometry('660x480')
        self.root.update()
        self.ui.show_settings_page('packages')
        self.root.update()
        for widget in (self.ui.close_button,):
            bottom=widget.winfo_rooty()-window.winfo_rooty()+widget.winfo_height()
            self.assertLessEqual(bottom,window.winfo_height())
            self.assertTrue(widget.winfo_ismapped())
        self.ui.show_settings_page('appearance')
        self.root.update()
        for button in self.ui.settings_accent_buttons.values():
            self.assertTrue(button.winfo_ismapped())
            bottom=button.winfo_rooty()-window.winfo_rooty()+button.winfo_height()
            self.assertLessEqual(bottom,window.winfo_height())
            right=button.winfo_rootx()-window.winfo_rootx()+button.winfo_width()
            self.assertLessEqual(right,window.winfo_width())


if __name__=='__main__':
    unittest.main()
