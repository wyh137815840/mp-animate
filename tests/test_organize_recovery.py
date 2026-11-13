"""大包整理回归：只搬本集字幕，花絮不得当正片。"""
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "plugins.v2" / "anime115"
pkg = types.ModuleType("anime115")
pkg.__path__ = [str(ROOT)]
pkg.__package__ = "anime115"
sys.modules.setdefault("anime115", pkg)
from anime115.anime_flatten import Node
from anime115.anime_jobs import _decide_move, _move_name, _move_budget, JobConfig


def node(name):
    return Node('/pack/' + name, name, False, 900000000)


class RecoveryTest(unittest.TestCase):
    def test_pack_subtitles_belong_to_each_episode(self):
        files = [node(f'Show - {i:02d}{suffix}') for i in (1, 2, 3)
                 for suffix in ('.mkv', '.SC.ass', '.TC.ass')]
        d = _decide_move({'show': 'Show', 'season': 1}, files, '/anime', 'Show 01-03')
        self.assertEqual(d['action'], 'move')
        self.assertEqual(len(d['nodes']), 9)
        for item in d['items']:
            self.assertEqual(len(item['nodes']), 3)
            self.assertTrue(all(f'{item["episode"]:02d}' in n.name for n in item['nodes']))
            self.assertEqual(len({_move_name(item, n) for n in item['nodes']}), 3)

    def test_numbered_pv_not_an_episode_or_version(self):
        files = [node('Show - 01.mkv'), node('Show - 02.mkv'), node('Show [PV][01].mkv')]
        d = _decide_move({'show': 'Show', 'season': 1}, files, '/anime', 'Show 01-02')
        self.assertEqual(d['pack_episodes'], 2)
        self.assertEqual(len(d['nodes']), 2)

    def test_main_plus_menu(self):
        files = [node('Show - 03.mkv'), node('Show MENU.mkv'), node('Show CM02.mkv')]
        d = _decide_move({'show': 'Show', 'season': 1, 'episode': 3}, files, '/anime')
        self.assertEqual(d['action'], 'move')
        self.assertEqual(d['name'], 'Show - 03.mkv')

    def test_all_promos_remain_unmoved(self):
        for files in ([node('Show PV01.mkv')], [node('Show MENU.mkv'), node('Show NCOP01.mkv')]):
            d = _decide_move({'show': 'Show', 'season': 1, 'episode': 1}, files, '/anime')
            self.assertEqual(d['action'], 'leave')

    def test_spy_and_prologue_title_are_not_extras(self):
        for title in ('SPY x FAMILY - 01.mkv', 'Prologue - 01.mkv'):
            d = _decide_move({'show': title, 'season': 1, 'episode': 1}, [node(title)], '/anime')
            self.assertEqual(d['action'], 'move')

    def test_other_episode_does_not_complete_task_after_promo_filter(self):
        files = [node('Show - 05.mkv'), node('Show PV03.mkv')]
        d = _decide_move({'show': 'Show', 'season': 1, 'episode': 3}, files, '/anime')
        self.assertEqual(d['action'], 'leave')

    def test_realistic_pack_moves_each_source_once(self):
        from test_pack_split import _pack_session, _task, PACK
        from anime115.anime_jobs import run_organize
        files = [Node(f'{PACK}/Show - {i:02d}{s}', f'Show - {i:02d}{s}', False, 1000)
                 for i in (1, 2, 3) for s in ('.mkv', '.SC.ass', '.TC.ass')]
        session = _pack_session(files)
        remaining = {n.path for n in files}
        original_move = session.move
        def move(n, dest, name):
            if n.path not in remaining:
                return False
            remaining.remove(n.path)
            return original_move(n, dest, name)
        session.move = move
        tasks = _task()
        result = run_organize(tasks, session, JobConfig(dry_run=False, library_root='/anime'))
        self.assertEqual(result['moves'][0]['moved_episodes'], 3)
        self.assertEqual(result['moves'][0]['failed_episodes'], 0)
        self.assertEqual(tasks[0]['status'], 'moved')
        self.assertEqual(len(session.moves), 9)
        self.assertEqual(remaining, set())

    def test_mini_and_sp_are_not_regular_versions(self):
        files = [node('Show - 01.mkv'), node('Show - 02.mkv'),
                 node('Show Mini [01].mkv'), node('Show - SP02.mkv')]
        d = _decide_move({'show': 'Show', 'season': 1}, files, '/anime', 'Show 01-02 + Mini')
        self.assertEqual(len(d['nodes']), 2)
        self.assertTrue(all(len(i['videos']) == 1 for i in d['items']))

    def test_organizer_attempts_unvisited_tasks_before_old_collisions(self):
        from test_pack_split import _Session
        from anime115.anime_jobs import run_organize
        from unittest.mock import patch
        ts = [{'infohash': str(i) * 40, 'status': 'submitted', 'organize_attempted_at': t}
              for i, t in ((1, 123), (2, 0), (3, 50))]
        session = _Session()
        session.offline = [{'info_hash': t['infohash'], 'status': 2} for t in ts]
        order = []
        def organize(t, session, library, config, moves, offline, round_deadline=None):
            order.append(t['infohash'][0])
            moves.append({'action': 'leave', 'reason': 'collision'})
        with patch('anime115.anime_jobs._organize_one', side_effect=organize):
            run_organize(ts, session, JobConfig(dry_run=False, library_root='/anime'))
        self.assertEqual(order, ['2', '3', '1'])
        self.assertTrue(all(t['organize_attempted_at'] > 123 for t in ts))

    def test_tc_language_is_preserved_locally(self):
        from anime115.anime_subs import subtitle_relative
        self.assertEqual(subtitle_relative('Show/S01E01.strm', 'Show.TC.ass'), 'Show/S01E01.zh-TW.ass')

    def test_budget_is_bounded_and_scales(self):
        cfg = JobConfig()
        self.assertGreater(_move_budget(cfg, 72), _move_budget(cfg, 1))
        self.assertLessEqual(_move_budget(cfg, 10000), 900)


if __name__ == '__main__':
    unittest.main()
