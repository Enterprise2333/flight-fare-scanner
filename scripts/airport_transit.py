"""中转机场中文名称与保守行程规划建议。

`recommended_minutes` 不是机场 MCT（最低转机时间），也不构成承运人保障。
它用于帮助旅客判断报价中的实际中转是否偏紧。数值和理由来自公开机场、航司
或入境机构页面；购票前仍须以同一票号的出票页和承运人规则为准。
"""

# 仅收录已通过公开来源核验、且当前报告覆盖到的主要中转机场。
TRANSIT_GUIDES = {
    "DOH": {
        "name_zh": "多哈哈马德国际机场",
        "recommended_minutes": 120,
        "reason": "同一票号转机的保守规划建议为 2 小时；实际最低衔接须以承运航司及出票页为准。",
        "source_url": "http://dohahamadairport.com/airport-guide/at-the-airport/transfers",
    },
    "JFK": {
        "name_zh": "纽约约翰·F·肯尼迪国际机场",
        "recommended_minutes": 180,
        "reason": "美国首入境建议预留至少 3 小时，以完成入境、提取托运行李、海关及重新托运。",
        "source_url": "https://www.jfkairport.com/flights/connections-guide",
    },
    "LAX": {
        "name_zh": "洛杉矶国际机场",
        "recommended_minutes": 180,
        "reason": "美国首入境建议预留至少 3 小时；通常须取出托运行李并经海关后重新托运。",
        "source_url": "https://www.help.cbp.gov/s/article/Article-1244?language=en_US",
    },
    "MAD": {
        "name_zh": "马德里-巴拉哈斯阿道弗·苏亚雷斯机场",
        "recommended_minutes": 120,
        "reason": "规划建议预留 2 小时；Iberia 公布部分衔接最低时间为 55 分钟，跨航站楼或安检时应留余量。",
        "source_url": "https://www.iberia.com/gb/fly-with-iberia/madrid-airport/connectiontime/",
    },
    "CDG": {
        "name_zh": "巴黎戴高乐机场",
        "recommended_minutes": 120,
        "reason": "规划建议预留 2 小时；机场对不足 1 小时的衔接提供短转机协助，说明短衔接风险较高。",
        "source_url": "https://www.parisaeroport.fr/en/passengers/flights/connecting-flights",
    },
    "AMS": {
        "name_zh": "阿姆斯特丹史基浦机场",
        "recommended_minutes": 120,
        "reason": "规划建议预留 2 小时；KLM 公布非申根最低转机时间为 50 分钟，护照或安检排队可能增加时间。",
        "source_url": "https://www.klm.co.uk/information/airport/transfers",
    },
}


def display_name(code):
    """返回“中文机场名（IATA）”，未收录时保留原始 IATA。"""
    guide = TRANSIT_GUIDES.get(str(code or "").upper())
    return "{0}（{1}）".format(guide["name_zh"], code) if guide else str(code or "?")


def guide_for(code):
    """获取中转建议；调用方不得将缺失项解释为可安全短转。"""
    return TRANSIT_GUIDES.get(str(code or "").upper())
