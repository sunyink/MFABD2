"""Offline OCR-pattern regressions; no game connection or OCR-engine acceptance.

Run: python -m unittest discover -s tools -p test_shared_ocr.py
Text examples use the 2026-10-29 game text export, existing PC wording and
mixed-script OCR output. Legacy image labels still require live-game validation.
"""
import json
from pathlib import Path
import re
import unittest


RESOURCE = Path(__file__).resolve().parents[1] / 'assets/resource'


def load_pack(pack):
    nodes = {}
    for path in sorted((RESOURCE / pack / 'pipeline').glob('*.json')):
        if path.name.startswith('.'):
            continue
        nodes.update(json.loads(path.read_text(encoding='utf-8')))
    return nodes


class SharedOcrPatterns(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = load_pack('base')
        cls.pc = load_pack('pc')

    def node(self, name, pc=False):
        # These checks use top-level OCR nodes; inline lists are inspected directly.
        data = dict(self.base[name])
        if pc:
            data.update(self.pc.get(name, {}))
        return data

    def matches(self, name, text, pc=False):
        data = self.node(name, pc)
        for pattern, replacement in data.get('replace', []):
            text = re.sub(pattern, replacement, text)
        expected = data['expected']
        if isinstance(expected, str):
            expected = [expected]
        return any(re.search(pattern, text) for pattern in expected)

    def assert_examples(self, name, positive, negative=()):
        for pc in (False, True):
            for text, wanted in [(t, True) for t in positive] + [(t, False) for t in negative]:
                with self.subTest(node=name, pc=pc, text=text):
                    self.assertEqual(wanted, self.matches(name, text, pc))

    def test_database_localizations_and_empirical_simplified_aliases(self):
        samples = {
            'Arbitrage_Bargaining_SkillList': ('砍价', '討價還價'),
            'Arbitrage_Buy_Select_QR3': ('美丽无望', '不可能的美麗'),
            'Arbitrage_Buy_Select_QE2': ('恶梦之冬', '夢靨冬季'),
            'Arbitrage_Buy_Select_QE5': ('戏水女', '戲水女王'),
            'Arbitrage_PriceList_Open_Entry': ('价目表', '販售行情表'),
            'Collect_WayPoint_Board': ('探索告示板', '任務布告欄'),
            'Collect_QuickMove_ToBoard': ('探索告示板', '任務布告欄'),
            'QuickHunt_CollectMapHuntingGrounds_9': ('守山人休息处', '守山人的休息處'),
            'Equip_EquipUp_CK': ('级别', '等级', '等級'),
            'Equip_filter_SubMenu': ('装备筛选', '裝備篩選'),
            'Equip_Sequen_MenuFind': ('自定义标记', '自訂標記'),
            'StartGame_PayFailed': ('支付失败', '付款失敗'),
            'StartGame_Error_API': ('API服务器连线发生错误。请重新尝试。', '與API伺服器連線時發生錯誤。'),
            'StartGame_Update_Data_DownLoading': ('正在下载', '下载中', '下載中'),
            'Event_MiniGame_Run_Goin': ('小活动', '迷你遊戲'),
            'Rec_<AutoBattle_Is_Auto>_Ocr_01': ('自动战斗中', '自動戰鬥中', '自動戰鬥結束'),
        }
        for name, texts in samples.items():
            self.assert_examples(name, texts)

    def test_existing_pc_wording_is_shared_verbatim(self):
        # PC 原文里截短的词、混写的词都是实际可命中的形态，下放时不改写成数据库全文。
        cases = {
            'Collect_QuickCart_SelectType_StoryPack': ('故事遊戲卡', '故事遊戲卡帶'),
            'Collect_FirstPack': ('未插好遊戲卡', '未插好遊戲卡帶'),
            'Global_BackToPve_CardMenu_Tp1': ('遊戲卡帶收藏集',),
            'Rec_<SeasonEvent_MamonoFollow_Practice>_Ocr_01': ('距離開始還', '距離開始還有3天'),
            'Rec_<Event_Event_Main>_Ocr_01': ('活動服装', '活動服裝'),
            'Rec_<Daily_Busin_Claim>_Ocr': ('經营管理', '經營管理'),
            'Equip_Skill': ('製作装備', '製作裝備'),
            'Equip_TouchRefining': ('煉製', '炼製'),
            'GachaADV_GachaResultBack': ('抽抽樂點数', '抽抽樂點數'),
            'StartGame_7dAD_Check': ('7日期間不再觀看', '7天內不再顯示'),
            'GachaADV_5X': ('艾莉诺', '艾莉諾', '這是不用多說的巨作'),
            'PVP_SeasonSettling': ('结算完成后确认', '結算', '結算中'),
            'StartGame_Update_Data': ('将下载游戏', '下載遊戲', '所需资料', '所需資料'),
        }
        for name, texts in cases.items():
            self.assertNotIn('expected', self.pc.get(name, {}), name)
            self.assert_examples(name, texts)

    def test_mixed_script_ocr_output_still_matches(self):
        # 识别结果常把个别字认成另一套字形，字符类与模糊前瞻写法就是为此保留的。
        samples = {
            'Arbitrage_Merchant_Close_Ck': ('關闭商店', '关閉商店'),
            'Arbitrage_Buy_Select_QC19': ('被遗忘的戰争',),
            'Arbitrage_Buy_Select_QE2': ('夢魘冬季', '梦靨冬季'),
            'Collect_QuickMove_ToTavern': ('酒館', '酒馆'),
            'Rec_QuickMove_Menu': ('狩猎場', '旅館'),
            'Daily_Busin_Entered': ('格鲁菲餐廳', '格魯菲餐厅'),
            'Collect_ClickTelepor_ButNotActive': ('没有已啟用的魔法陣。',),
            'QuickHunt_CollectMapHuntingGrounds_7': ('蜥蜴人祭坛', '蜥蜴人祭壇'),
            'Collect_Skill3_1_ERR_TooClose': ('无法在魔法阵附近使用天赋技能。', '在此地點無法使用天賦技能。'),
            'SellFish_Step2_NoFish': ('没有可出售的道具。', '沒有可販售的道具。'),
            'Redemption_Evil_Redemption_In': ('救赎之塔', '救贖之塔'),
            'PVP_Multiplier_Adjust_Set': ('设置鲜血鸡尾酒消耗量', '血之雞尾酒消耗量設定'),
        }
        for name, texts in samples.items():
            self.assert_examples(name, texts)

    def test_map_ocr_digit_replacement_does_not_break_titles(self):
        for pc in (False, True):
            for text in ('达雷普镇', '達雷普鎮', '学校1楼', '學校1樓', '恶梦之城I', '惡夢城市1'):
                with self.subTest(pc=pc, text=text):
                    self.assertTrue(self.matches('Collect_TargetMap1-2', text, pc))
            self.assertFalse(self.matches('Collect_TargetMap1-2', '达普雷镇', pc))

    def test_traditional_battle_numbers_share_the_simplified_patterns(self):
        # 戰/鬥 由 replace 归一成简体，再走原有的编号正则，不另写一套繁体正则。
        self.assert_examples('Collect_TargetMap2', ('戰鬥I', '戰鬥 I', '戰鬥Ⅰ', '战斗1', '盧戈森林'))
        self.assert_examples('Collect_TargetMap3', ('戰鬥II', '戰鬥Ⅱ', '戰鬥Ⅱ禁閉室', '战斗 II', '盧戈森林深處'))

    def test_pc_inherits_later_map_entry_and_finish_conditions(self):
        for name in ('Collect_TargetMap3', 'Cpt_Collect_TargetMapEnd_Ocr'):
            self.assertNotIn('expected', self.pc[name])
            self.assert_examples(name, (
                '战斗Ⅱ禁闭室', '戰鬥Ⅱ禁閉室', '第四记忆空间', '第四個記憶空間',
            ))

    def test_pc_only_entry_route_stays_in_pc(self):
        # 活动横幅入口只有 PC 走，识别词与点击一起留在 PC，BASE 不多出孤立节点。
        self.assertNotIn('Event_GoinEvent_OcrCk', self.base)
        node = self.pc['Event_GoinEvent_OcrCk']
        self.assertEqual('OCR', node['recognition'])
        self.assertEqual('Click', node['action'])
        for text in ('魔兽追踪者', '魔獸追蹤者', '开始', '開始', '解锁', '解鎖'):
            self.assertTrue(any(re.search(p, text) for p in node['expected']), text)

    def test_guessed_pvp_settlement_is_left_unchanged(self):
        self.assertEqual('结算中', self.base['PVP_Results']['expected'])
        self.assertEqual(['结算中', '結算'], self.pc['PVP_Results']['expected'])

    def test_free_plus_five_banner_requires_the_literal_bonus(self):
        self.assert_examples('Activities_FreeClothing', (
            '强化免费赠送', '强化服装的机会', '+5强化',
            '獲得限定+5', '獲得限定[+5]', '強化服裝的機會',
        ), ('獲得限定5', '獲得限定+'))

    def test_existing_simplified_regex_alternatives_are_preserved(self):
        samples = {
            'Arbitrage_Bargaining_SkillList': ('砍价', '砍價', '讨价还价'),
            'Arbitrage_Merchant_Egress': ('出售', '贩售', '购买'),
            'Arbitrage_Buy_Select_QC7': ('愤怒天使', '愤怒的天使', '憤怒天使'),
            'Arbitrage_Buy_Select_QC9': ('铁假面', '铁面具', '鐵假面'),
            'Arbitrage_Buy_Select_QC15': ('复仇的誓言', '复仇的誓约'),
            'Arbitrage_Buy_Select_QR3': ('美丽无望', '不可能的美丽'),
            'Arbitrage_Buy_Select_QR6': ('御剑传', '剑传'),
            'Arbitrage_Buy_Select_QE1': ('夏日骑士', '夏天骑士'),
            'Arbitrage_Buy_Select_QE2': ('恶梦之冬', '梦魇冬季'),
            'Arbitrage_Buy_Select_QE3': ('海滨天使', '海边天使'),
            'Arbitrage_PriceList_Open_Entry': ('价目表', '贩售行情表'),
            'Arbitrage_PriceList_Sort_Select': ('溢价率', '加价率'),
            'Arbitrage_Cooking_SubMenu': ('立刻恢复', '立即恢复', '擁有'),
            'QuickHunt_CollectMapCrystalCaveCK': ('火之洞穴', '水的洞穴', '風的洞穴'),
            'GachaADV_PageEnter_1#': ('查看概率', '查看机率', '查看機率'),
            'SeasonEvent_MiniGame_Quiz_Entry': ('小活动', '迷你游戏', '迷你遊戲'),
            'SeasonEvent_MiniGame_Quiz_Enter': ('开始答题', '解题', '解題'),
        }
        for name, texts in samples.items():
            self.assert_examples(name, texts)

    def test_purchase_labels_keep_numeric_and_whole_label_boundaries(self):
        self.assert_examples('Agt_BuyQuantity_Available_Ocr',
                             ('可购买1个', '可購買1,234個', ' 可购买 1，234 个 ', '可购買400個'),
                             ('可购买个', '可購買全部', '已购买1个', '可购买1个以上'))
        self.assert_examples('Agt_BuyConfirm_Ocr', ('购买', '購買', ' 購買 ', '购買'), ('购买全部收藏', '不可购买'))

    def test_skill_grades_take_one_expected_slot_each(self):
        # order_by=Expected 按条目序号排先后，每一档必须只占一个位置。
        normal = [('传说', '傳說'), ('达人', '達人'), ('高手',), ('进阶', '進階'), ('升星',), ('初级', '初級', '新手')]
        teleport = [normal[i] for i in (1, 2, 3, 5, 4, 0)]
        for name, grades in (
            ('Rec_<Setup_SetSkill_3-1_Pick>_Rank', normal),
            ('Rec_<Setup_SetSkill_3-4_Pick>_Rank', teleport),
        ):
            patterns = self.node(name)['expected']
            self.assertEqual(len(grades), len(patterns), name)
            for index, grade in enumerate(grades):
                for word in grade:
                    hits = [i for i, pattern in enumerate(patterns) if re.search(pattern, word)]
                    self.assertEqual([index], hits, (name, word))

    def test_pc_inline_ocr_filters_are_preserved(self):
        for name, path in (
            ('Arbitrage_Favorit_Buy', ('all_of', 0)),
            ('BagScan_TabReset_Coll', ('all_of', 1)),
            ('Collect_C3_Jail', ('all_of', 0)),
            ('Collect_QuickMove_AtTavern', ('all_of', 1, 'any_of', 1)),
        ):
            data = self.pc[name]
            for part in path:
                data = data[part]
            self.assertEqual('OCR', data['recognition'])
            self.assertTrue(data['expected'], name)


if __name__ == '__main__':
    unittest.main()
