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
        apps=[p.App('APT',f'app-{i:03d}',f'Application {i:03d}',version='1.0') for i in range(100)]
        apps.append(p.App('APT','bash','Bash',protected=True))
        self.mock=patch.object(p,'scan_all',return_value=(apps,[]))
        self.mock.start()
        self.ui=p.PocketUI(self.root)
        end=time.monotonic()+5
        while self.ui.busy and time.monotonic()<end:
            self.root.update()
            time.sleep(.02)
        self.assertFalse(self.ui.busy)
        self.root.focus_force()
        self.ui.tree.focus_set()
        self.root.update()

    def tearDown(self):
        for item in self.root.tk.splitlist(self.root.tk.call('after','info')):
            self.root.after_cancel(item)
        self.root.destroy()
        self.mock.stop()

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

if __name__=='__main__':
    unittest.main()
