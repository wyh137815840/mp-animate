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
        files = [Node(f'{PACK}/Kakkou no Iinazuke S2 - {i:02d}{s}', f'Kakkou no Iinazuke S2 - {i:02d}{s}', False, 1000)
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

    def test_partial_pack_resumes_only_with_matching_declared_range(self):
        files = [node('Show - 01.mkv'), node('Show - 03.mkv')]
        d = _decide_move({'show': 'Show', 'season': 1, 'title': 'Show [01-03]'}, files, '/anime', '/pack/Show [01-03]')
        self.assertEqual([i['episode'] for i in d['items']], [1, 3])
        from anime115.anime_jobs import _pack_items
        self.assertEqual(_pack_items(files, {'season': 1, 'title': 'Show [01-03]'}, '/pack/Show [01-12]', files), [])

    def test_alternate_short_series_and_romanized_preview_are_not_main(self):
        files = [node(f'[Group] Kakkou no {series} [{i:02d}]{s}')
                 for series in ('Iinazuke', 'Iikagen') for i in (1, 2)
                 for s in ('.mkv', '.SC.ass', '.TC.ass')]
        files.append(node('[Group] Kakkou no Iinazuke [Yokoku][01].mkv'))
        d = _decide_move({'show': '杜鹃的婚约', 'season': 1, 'title': 'Kakkou no Iinazuke [01-24+Mini_01-22]'}, files, '/anime', 'Kakkou no Iinazuke [01-24]')
        self.assertEqual(len(d['nodes']), 6)
        self.assertTrue(all('Iinazuke' in n.name and 'Yokoku' not in n.name for n in d['nodes']))
        self.assertTrue(all(len(i['subs']) == 2 for i in d['items']))

    def test_unknown_mixed_series_pack_is_not_split(self):
        from anime115.anime_jobs import _pack_items
        files = [node('Show A - 01.mkv'), node('Show B - 02.mkv')]
        self.assertEqual(_pack_items(files, {'season': 1}, 'Unknown', files), [])

    def test_remaining_short_series_alone_is_not_main(self):
        from anime115.anime_jobs import _pack_items
        files = [node('[Group] Kakkou no Iikagen [01].mkv'), node('[Group] Kakkou no Iikagen [02].mkv')]
        self.assertEqual(_pack_items(files, {'season': 1, 'title': 'Kakkou no Iinazuke [01-24]'}, 'Kakkou no Iinazuke [01-24]', files), [])

    def test_subtitles_use_lan_without_changing_public_video_urls(self):
        from anime115.anime_jobs import _subtitle_url, _play_url
        cfg = JobConfig(play_base='https://mp.example/redirect?pickcode={pickcode}&file_name={filename}',
                        play_base_lan='http://192.168.1.9:3000/redirect?pickcode={pickcode}&file_name={filename}', strm_for='public')
        self.assertTrue(_play_url(cfg, 'pc', 'file.mkv').startswith('https://mp.example/'))
        self.assertTrue(_subtitle_url(cfg, 'pc', 'file.SC.ass').startswith('http://192.168.1.9:3000/'))
        cfg.play_base_lan = ''
        self.assertTrue(_subtitle_url(cfg, 'pc', 'file.SC.ass').startswith('https://mp.example/'))

    def test_existing_subtitle_is_not_downloaded_or_overwritten(self):
        import tempfile
        from unittest.mock import patch
        from anime115.anime_jobs import _apply_subtitles
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder) / 'Show' / 'S01E01.zh-CN.ass'
            p.parent.mkdir(); p.write_text('existing valid subtitle')
            sub = Node('/cloud/Show.SC.ass', 'Show.SC.ass', False, 100, raw={'pc': 'pc'})
            cfg = JobConfig(strm_root=folder, play_base='http://example/redirect?pickcode={pickcode}', subtitle_enabled=True)
            with patch('anime115.anime_jobs.fetch_bytes') as fetch:
                _apply_subtitles({}, [sub], cfg, 'Show/S01E01.strm', write=True)
                fetch.assert_not_called()
            self.assertEqual(p.read_text(), 'existing valid subtitle')

    def test_subdir_exact_name_wins_over_short_series_same_number(self):
        from anime115.anime_subs import match_subtitles
        video = node('[Group] Main Show [08].mkv')
        subs = [Node('/pack/Subs/' + name, name, False, 100) for name in
                ('[Group] Short Show [08].SC.ass', '[Group] Main Show [08].SC.ass', '[Group] Main Show [08].TC.ass')]
        matched = match_subtitles([video, *subs], video, 8, 1)
        self.assertEqual([s.name for s in matched], ['[Group] Main Show [08].SC.ass', '[Group] Main Show [08].TC.ass'])

    def test_ass_internal_title_guards_renamed_short_series(self):
        from anime115.anime_subs import ass_title_matches
        main = '[Group] Kakkou no Iinazuke [08][BDRip].zh-CN.ass'
        self.assertFalse(ass_title_matches(b'[Script Info]\nTitle: [Group] Kakkou no Iikagen [08][BDRip].SC', main))
        self.assertTrue(ass_title_matches(b'[Script Info]\nTitle: [Group] Kakkou no Iinazuke [08][BDRip].SC', main))
        self.assertFalse(ass_title_matches(b'[Script Info]\nTitle: [Group] Kakkou no Iinazuke [09][BDRip].SC', main))
        self.assertTrue(ass_title_matches(b'[Script Info]\nTitle: Default Aegisub file', main))

    def test_nested_probe_prefers_name_matching_directory(self):
        from anime115.anime_jobs import _choose_by_nested_id
        from anime115.anime_flatten import Node
        probes = []

        class _S:
            def list_dir(self, path):
                probes.append(path)
                if path == '/inbox/[Group] Wanted - 04':
                    return [Node(path + '/wanted.mkv', 'wanted.mkv', False, 1, raw={'file_id': 'fid-1'})]
                return []
        children = [Node('/inbox/Other Pack - 01', 'Other Pack - 01', True),
                    Node('/inbox/[Group] Wanted - 04', '[Group] Wanted - 04', True),
                    Node('/inbox/Third - 02', 'Third - 02', True)]
        chosen, why = _choose_by_nested_id(_S(), children, 'fid-1', None, ['[Group] Wanted - 04'])
        self.assertEqual(chosen.name, '[Group] Wanted - 04')
        self.assertEqual(probes[0], '/inbox/[Group] Wanted - 04')
        self.assertEqual(why, '')

    def test_stall_reason_is_recorded_on_the_task(self):
        from anime115.anime_jobs import run_organize
        from test_pack_split import _Session
        session = _Session()
        session.offline = [{'info_hash': 'f' * 40, 'status': 2}]
        task = {'infohash': 'f' * 40, 'show': '佐藤同学', 'season': 1, 'episode': 3,
                'title': '佐藤同学 - 03', 'folder': '/inbox', 'status': 'submitted'}
        result = run_organize([task], session, JobConfig(dry_run=False, library_root='/动漫'))
        self.assertEqual(result['moves'][0]['action'], 'leave')
        self.assertTrue(task.get('organize_reason'))
        self.assertIn('organize_checked_at', task)

    def test_unnumbered_extras_do_not_block_the_main_episode(self):
        files = [node('[DAY][女神のカフェテラス][第20話 「冗談」][BDrip][1080P].mkv'),
                 node('[DAY][女神のカフェテラス][第21話 「母と娘」][BDrip][1080P].mkv'),
                 node('PV①.mkv'), node('PV②.mkv'), node('Vol.4.mkv')]
        d = _decide_move({'show': '女神的露天咖啡厅', 'season': 2, 'episode': 0}, files, '/anime', '女神的露天咖啡厅[BD]')
        self.assertEqual(d['action'], 'move')
        self.assertEqual([i['episode'] for i in d['items']], [20, 21])
        self.assertTrue(all('PV' not in n.name and 'Vol.' not in n.name for n in d['nodes']))

    def test_all_unnumbered_special_stays_put(self):
        files = [node('[Snow-Raws] Mob Psycho 100 OAD (BD 1920x1080 FLACx2).mkv'),
                 node('[Snow-Raws] Mob Psycho 100 OAD NCOP (BD 1920x1080 FLAC).mkv'),
                 node('[Snow-Raws] Mob Psycho 100 OAD MENU (BD 1920x1080 FLAC).mkv')]
        d = _decide_move({'show': 'Mob Psycho 100', 'season': 2, 'episode': 2}, files, '/anime')
        self.assertEqual(d['action'], 'leave')

    def test_volume_file_is_not_an_episode(self):
        from anime115.anime_jobs import _pack_episode_full
        self.assertEqual(_pack_episode_full('Vol.4.mkv'), 0)
        self.assertEqual(_pack_episode_full('[BD] Show Vol.1 [1080p].mkv'), 0)
        self.assertEqual(_pack_episode_full('Show - 04 [1080p].mkv'), 4)

    def test_multi_season_pack_is_not_split_into_one_season(self):
        from anime115.anime_jobs import _pack_items
        files = [node('[DAY][女神のカフェテラス][第20話][BDrip].mkv'),
                 node('[DAY][女神のカフェテラス][第21話][BDrip].mkv'),
                 node('[DAY][女神のカフェテラス][第22話][BDrip].mkv')]
        self.assertEqual(_pack_items(files, {'season': 2}, '女神的露天咖啡厅S1+S2[BD]', files), [])

    def test_series_labels_survive_real_release_names(self):
        from anime115.anime_jobs import _pack_label_in, _pack_series_labels
        cases = [
            ('[Nekomoe kissaten][Goblin Slayer][12END][1080p][CHS].mp4',
             '[Nekomoe kissaten][Goblin Slayer][01-12END][1080p][CHS]', True),
            ('[Nekomoe kissaten] Kakkou no Iikagen [08][BDRip 1080p HEVC-10bit AAC].mkv',
             '[Nekomoe kissaten][Kakkou no Iinazuke][BDRip]', False),
            ('[DAY][女神のカフェテラス][第20話][BDrip][1080P][X264 FLAC].mkv',
             '[DAY字幕组][女神的露天咖啡厅][BD][简日双语][BDrip][1080P][MKV][FLAC]', True),
            ('[八重樱字幕组][噬血狂袭OVAIV][Strike the Blood OVA IV][01][1080P][简体].mp4',
             '[八重樱字幕组][噬血狂袭OVAIV][01-04][1080P][简体]', True),
        ]
        for name, pack, expected in cases:
            labels = _pack_series_labels(name)
            hit = any(_pack_label_in(pack, label) for label in labels)
            self.assertEqual(hit, expected, f'{name} -> {labels}')

    def test_episode_beyond_season_cap_is_refused(self):
        caps = {('女神的露天咖啡厅', 2): 12}
        files = [node('[DAY][女神のカフェテラス][第20話][BDrip][1080P].mkv'),
                 node('[DAY][女神のカフェテラス][第21話][BDrip][1080P].mkv')]
        d = _decide_move({'show': '女神的露天咖啡厅', 'season': 2, 'episode': 0}, files, '/anime',
                          '女神的露天咖啡厅[BD]', caps)
        self.assertEqual(d['action'], 'leave')
        self.assertIn('上限', d['reason'])
        inside = _decide_move({'show': '女神的露天咖啡厅', 'season': 2, 'episode': 0},
                               [node('[DAY][女神のカフェテラス][第11話][BDrip].mkv'),
                                node('[DAY][女神のカフェテラス][第12話][BDrip].mkv')],
                               '/anime', '女神的露天咖啡厅[BD]', caps)
        self.assertEqual(inside['action'], 'move')
        self.assertEqual([i['episode'] for i in inside['items']], [11, 12])

    def test_season_subfolders_split_pack_into_seasons(self):
        from anime115.anime_jobs import _pack_items
        files = [Node('/pack/BD/S1/[DAY][女神のカフェテラス][第1話][BDrip].mkv', '[DAY][女神のカフェテラス][第1話][BDrip].mkv', False, 100),
                 Node('/pack/BD/S1/[DAY][女神のカフェテラス][第2話][BDrip].mkv', '[DAY][女神のカフェテラス][第2話][BDrip].mkv', False, 100),
                 Node('/pack/BD/S2/[DAY][女神のカフェテラス][第13話][BDrip].mkv', '[DAY][女神のカフェテラス][第13話][BDrip].mkv', False, 100),
                 Node('/pack/BD/S2/[DAY][女神のカフェテラス][第14話][BDrip].mkv', '[DAY][女神のカフェテラス][第14話][BDrip].mkv', False, 100)]
        self.assertEqual(_pack_items(files, {'season': 2}, '女神のカフェテラス[BD]', files), [])

    def test_single_season_subfolder_pack_is_split(self):
        from anime115.anime_jobs import _pack_items
        files = [Node('/pack/BD/S2/[DAY][女神のカフェテラス][第13話][BDrip].mkv', '[DAY][女神のカフェテラス][第13話][BDrip].mkv', False, 100),
                 Node('/pack/BD/S2/[DAY][女神のカフェテラス][第14話][BDrip].mkv', '[DAY][女神のカフェテラス][第14話][BDrip].mkv', False, 100)]
        items = _pack_items(files, {'season': 2}, '女神のカフェテラス[BD]', files)
        self.assertEqual([i['season'] for i in items], [2, 2])
        self.assertEqual([i['episode'] for i in items], [13, 14])

    def test_tc_language_is_preserved_locally(self):
        from anime115.anime_subs import subtitle_relative
        self.assertEqual(subtitle_relative('Show/S01E01.strm', 'Show.TC.ass'), 'Show/S01E01.zh-TW.ass')

    def test_budget_is_bounded_and_scales(self):
        cfg = JobConfig()
        self.assertGreater(_move_budget(cfg, 72), _move_budget(cfg, 1))
        self.assertLessEqual(_move_budget(cfg, 10000), 900)


if __name__ == '__main__':
    unittest.main()
