"""Static transition regression: python -m unittest discover -s tools -p test_startgame_hot_update.py"""
import json
from pathlib import Path
import re
import unittest


class HotUpdateTransitions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1] / 'assets/resource/base/pipeline/StartGame.json'
        cls.nodes = json.loads(path.read_text(encoding='utf-8'))

    def test_running_app_can_resume_hot_update(self):
        following = self.nodes['StartGame_Check_App_Alive']['next']
        for node in ('StartGame_Update_Data', 'StartGame_Update_Data_DownLoading', 'StartGame_Loading'):
            self.assertLess(following.index(node), following.index('Global_Null'))

    def test_launch_entries_can_resume_hot_update(self):
        # 共享 base 启动入口需要接回更新流程；NT PC 的独立入口另行检查。
        for entry in ('StartGame_RunApp', 'StartGame_RunApp_Shell'):
            following = self.nodes[entry]['next']
            for node in ('StartGame_Update_Data', 'StartGame_Update_Data_DownLoading', 'StartGame_Loading'):
                self.assertLess(following.index(node), following.index('StartGame_Logo'), entry)

    def test_nt_pc_window_entry_can_resume_hot_update(self):
        # NT PC 使用独立的窗口检查入口，不经过 base 的 RunApp 分支。
        path = Path(__file__).resolve().parents[1] / 'assets/resource/pc/pipeline/StartGame.json'
        nodes = json.loads(path.read_text(encoding='utf-8'))
        self.assertIn('StartGame_Check_Win32_SetSize', nodes['StartGame_Start']['next'])
        following = nodes['StartGame_Check_Win32_SetSize']['next']
        for node in ('StartGame_Update_Data', 'StartGame_Update_Data_DownLoading', 'StartGame_Loading'):
            self.assertIn(node, following)
            self.assertLess(following.index(node), following.index('StartGame_Logo'))

    def test_download_can_finish_at_loading_screen(self):
        for node in ('StartGame_Update_Data_Click', 'StartGame_Update_Data_DownLoading'):
            self.assertIn('StartGame_Loading', self.nodes[node]['next'])
            self.assertIn('StartGame_TouchStart', self.nodes[node]['next'])

    def test_maintenance_precedes_background_title_and_loading(self):
        """维护公告居中且不遮挡背景标题与启动中字样，各入口及加载节点需先判维护。"""
        for entry in (
            'StartGame_Check_App_Alive',
            'StartGame_RunApp',
            'StartGame_RunApp_Shell',
            'StartGame_LoadingPage',
            'StartGame_Loading',
            'StartGame_Update_Data_Click',
            'StartGame_Update_Data_DownLoading',
        ):
            following = self.nodes[entry]['next']
            self.assertIn('StartGame_GameIsMaint', following, entry)
            maint_idx = following.index('StartGame_GameIsMaint')
            for bg_node in (
                'StartGame_Update_Data_DownLoading',
                '[JumpBack]StartGame_Update_Data_DownLoading',
                'StartGame_Loading',
                'StartGame_LoadingPage',
            ):
                if bg_node in following:
                    self.assertLess(maint_idx, following.index(bg_node), f'{entry}: {bg_node}')

    def test_nt_pc_maintenance_precedes_background_title_and_loading(self):
        """PC 独立启动入口需先判维护，且热更新下载循环继承 base 的 next 顺序。"""
        path = Path(__file__).resolve().parents[1] / 'assets/resource/pc/pipeline/StartGame.json'
        nodes = json.loads(path.read_text(encoding='utf-8'))
        following = nodes['StartGame_Check_Win32_SetSize']['next']
        self.assertIn('StartGame_GameIsMaint', following)
        maint_idx = following.index('StartGame_GameIsMaint')
        for bg_node in ('StartGame_Loading', 'StartGame_LoadingPage'):
            self.assertLess(maint_idx, following.index(bg_node), bg_node)
        self.assertNotIn('next', nodes.get('StartGame_Update_Data_DownLoading', {}))

    def test_maintenance_expected_and_focus_log(self):
        """维护节点需覆盖实机『无法登入及使用游戏』文案，并在日志和通知中同时输出提示。"""
        maint = self.nodes['StartGame_GameIsMaint']
        self.assertEqual('StopTask', maint['action'])
        self.assertEqual(['游戏服务器维护中'], maint['focus']['start'])
        self.assertEqual(['游戏服务器维护中'], maint['focus']['toast'])
        self.assertIs(maint['focus']['aborted'], True)
        matches = lambda patterns, text: any(re.search(p, text) for p in patterns)
        for text in ('将于 10月22日（四）进行定期维护和更新。', '维护影响：将无法登入及使用游戏'):
            self.assertTrue(matches(maint['expected'], text), text)
        path = Path(__file__).resolve().parents[1] / 'assets/resource/pc/pipeline/StartGame.json'
        pc_node = json.loads(path.read_text(encoding='utf-8')).get('StartGame_GameIsMaint', {})
        pc_expected = pc_node.get('expected', maint['expected'])
        for text in (
            '将于 10月22日（四）进行定期维护和更新。',
            '维护影响：将无法登入及使用游戏',
            '將於 10月22日（四）進行定期維護和更新。',
            '維護影響：將無法登入及使用遊戲',
        ):
            self.assertTrue(matches(pc_expected, text), text)

    def test_download_timeout_does_not_request_apk_update(self):
        self.assertEqual(['StartGame_LoadingPage'], self.nodes['StartGame_Update_Data_DownLoading']['on_error'])
        self.assertEqual(['StartGame_UpdateCloseGame'], self.nodes['StartGame_Update_PGM_Click']['next'])

    def test_download_confirmation_supports_both_languages(self):
        for name, texts in (
            ('StartGame_Update_Data', ('将下载游戏所需数据。', '下載遊戲所需資料。')),
            ('StartGame_Update_Data_Click', ('下载', '下載')),
        ):
            patterns = self.nodes[name]['expected']
            for text in texts:
                self.assertTrue(any(re.search(pattern, text) for pattern in patterns), text)
        self.assertEqual('Click', self.nodes['StartGame_Update_Data_Click']['action'])

    def test_pvp_interrupt_ignores_download_dialog(self):
        # PVP_PVPMes 挂在 Global_ToSandBox 的 JumpBack 里，下载框背景的整行文字不能触发它
        path = Path(__file__).resolve().parents[1] / 'assets/resource/base/pipeline/PVP.json'
        expected = json.loads(path.read_text(encoding='utf-8'))['PVP_PVPMes']['expected']
        matches = lambda text: any(re.search(pattern, text) for pattern in expected)
        self.assertFalse(matches('正在确认下载容量100%'))
        self.assertTrue(matches('确认'))

    def test_nt_pc_pvp_interrupt_ignores_download_dialog(self):
        path = Path(__file__).resolve().parents[1] / 'assets/resource/pc/pipeline/PVP.json'
        pc_node = json.loads(path.read_text(encoding='utf-8')).get('PVP_PVPMes', {})
        base_path = path.parents[2] / 'base/pipeline/PVP.json'
        base_node = json.loads(base_path.read_text(encoding='utf-8'))['PVP_PVPMes']
        expected = pc_node.get('expected', base_node['expected'])
        matches = lambda text: any(re.search(pattern, text) for pattern in expected)
        for text in ('正在确认下载容量100%', '正在確認下載容量100%'):
            self.assertFalse(matches(text), text)
        for text in ('确认', '確認'):
            self.assertTrue(matches(text), text)


if __name__ == '__main__':
    unittest.main()
