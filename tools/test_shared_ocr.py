"""Offline OCR-pattern regressions; no game connection or OCR-engine acceptance.

Run: python -m unittest discover -s tools -p test_shared_ocr.py
Text examples follow the 2026-10-29 game text export. Numbered map labels also
exercise previously observed OCR variants, separately from database wording.
"""
import itertools
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
            'QuickHunt_CollectMapHuntingGrounds_9': ('守山人休息处', '守山人的休息處'),
            'Equip_EquipUp_CK': ('级别', '等级', '等級'),
            'Equip_filter_SubMenu': ('装备筛选', '裝備篩選'),
            'Equip_Sequen_MenuFind': ('自定义标记', '自訂標記'),
            'StartGame_PayFailed': ('支付失败', '付款失敗'),
            'StartGame_Error_API': ('API服务器连线发生错误。请重新尝试。', '與API伺服器連線時發生錯誤。'),
            'StartGame_Update_Data_DownLoading': ('正在下载', '下载中', '下載中'),
        }
        for name, texts in samples.items():
            self.assert_examples(name, texts)

    def test_map_ocr_digit_replacement_does_not_break_titles(self):
        for pc in (False, True):
            title = '达雷普镇' if pc else '达普雷镇'
            for text in (title, '達雷普鎮', '学校1楼', '學校1樓', '恶梦之城I', '惡夢城市1'):
                with self.subTest(pc=pc, text=text):
                    self.assertTrue(self.matches('Collect_TargetMap1-2', text, pc))

    def test_existing_simplified_regex_alternatives_are_preserved(self):
        samples = {
            'Arbitrage_Bargaining_SkillList': ('砍价', '讨价还价'),
            'Arbitrage_Merchant_Egress': ('出售', '贩售', '购买'),
            'Arbitrage_Buy_Select_QC7': ('愤怒天使', '愤怒的天使'),
            'Arbitrage_Buy_Select_QC9': ('铁假面', '铁面具'),
            'Arbitrage_Buy_Select_QC15': ('复仇的誓言', '复仇的誓约'),
            'Arbitrage_Buy_Select_QR3': ('美丽无望', '不可能的美丽'),
            'Arbitrage_Buy_Select_QR6': ('御剑传', '剑传'),
            'Arbitrage_Buy_Select_QE1': ('夏日骑士', '夏天骑士'),
            'Arbitrage_Buy_Select_QE2': ('恶梦之冬', '梦魇冬季'),
            'Arbitrage_Buy_Select_QE3': ('海滨天使', '海边天使'),
            'Arbitrage_PriceList_Open_Entry': ('价目表', '贩售行情表'),
            'Arbitrage_PriceList_Sort_Select': ('溢价率', '加价率'),
            'Arbitrage_Cooking_SubMenu': ('立刻恢复', '立即恢复'),
            'QuickHunt_CollectMapCrystalCaveCK': ('水之洞穴', '水的洞穴', '風的洞穴'),
            'GachaADV_PageEnter_1#': ('查看概率', '查看机率', '查看機率'),
            'SeasonEvent_MiniGame_Quiz_Entry': ('小活动', '迷你游戏', '迷你遊戲'),
            'SeasonEvent_MiniGame_Quiz_Enter': ('开始答题', '解题', '解題'),
        }
        for name, texts in samples.items():
            self.assert_examples(name, texts)

    def test_map_one_does_not_accept_later_battle_numbers_or_deeper_forest(self):
        positive = [prefix + suffix for prefix in ('战斗', '戰鬥')
                    for suffix in ('I', ' I', '1', 'Ⅰ', '!', '！', 'l', '|')]
        self.assert_examples('Collect_TargetMap2', positive + ['卢戈森林', '盧戈森林'], (
            '战斗II', '战斗 III', '戰鬥Ⅱ', '戰鬥Ⅲ', '战斗I2', '战斗Ia',
            '卢戈森林深处', '盧戈森林深處',
        ))

    def test_map_two_does_not_accept_other_battle_numbers(self):
        positive = [prefix + suffix for prefix in ('战斗', '戰鬥')
                    for suffix in ('II', ' II', 'Ⅱ', ' Ⅱ', '11', '!!', 'ⅠⅠ', 'l|')]
        self.assert_examples('Collect_TargetMap3', positive, (
            '战斗I', '戰鬥Ⅰ', '战斗III', '戰鬥Ⅲ', '战斗II3', '战斗IIa', '戰鬥ⅡI',
        ))

    def test_character_set_lookaheads_do_not_match_unrelated_messages(self):
        cases = (
            ('QuickHunt_CollectMapHuntingGrounds_7', ('蜥蜴人祭坛', '蜥蜴人祭壇'), ('蜥蜴人大司祭', '人们正在祭拜')),
            ('Collect_ClickTelepor_ButNotActive', ('没有已激活的魔法阵。', '沒有已啟用的魔法陣。'), ('已有正在進行的任務。', '没有可购买的商品')),
            ('Collect_Skill3_1_ERR_TooClose', ('无法在魔法阵附近使用天赋技能。', '在此地點無法使用天賦技能。'), ('没有可出战的角色。', '无法在当前探索中使用的天赋技能。')),
            ('SellFish_Step2_NoFish', ('没有可出售的道具。', '沒有可販售的道具。'), ('没有可选择的鱼。', '没有可出战的角色。')),
            ('Redemption_Evil_Redemption_In', ('救赎之塔', '救贖之塔'), ('救赎之路', '救贖之手')),
            ('PVP_Multiplier_Adjust_Set', ('设置鲜血鸡尾酒消耗量', '血之雞尾酒消耗量設定'), ('血刻印量产设施', '血刻印量產設施')),
        )
        for name, positive, negative in cases:
            self.assert_examples(name, positive, negative)

    def test_purchase_labels_keep_numeric_and_whole_label_boundaries(self):
        self.assert_examples('Agt_BuyQuantity_Available_Ocr', ('可购买1个', '可購買1,234個', ' 可购买 1，234 个 '),
                             ('可购买个', '可購買全部', '已购买1个', '可购买1个以上'))
        self.assert_examples('Agt_BuyConfirm_Ocr', ('购买', '購買', ' 購買 '), ('购买全部收藏', '不可购买'))

    def test_skill_grade_groups_keep_priority_across_languages(self):
        normal = [('传说', '傳說'), ('达人', '達人'), ('高手',), ('进阶', '進階'), ('升星',), ('初级', '新手')]
        teleport = [normal[i] for i in (1, 2, 3, 5, 4, 0)]
        for name, grades in (
            ('Rec_<Setup_SetSkill_3-1_Pick>_Rank', normal),
            ('Rec_<Setup_SetSkill_3-4_Pick>_Rank', teleport),
        ):
            patterns = self.node(name)['expected']
            positions = [[next(i for i, pattern in enumerate(patterns) if re.search(pattern, word))
                          for word in grade] for grade in grades]
            for high, low in itertools.combinations(positions, 2):
                self.assertLess(max(high), min(low), name)

    def test_pc_inline_ocr_filters_are_preserved(self):
        for name, path in (
            ('Arbitrage_Favorit_Buy', ('all_of', 0)),
            ('Arbitrage_Bag_TabReset_Coll', ('all_of', 1)),
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
