"""
测试 Cookie 提取用户信息
用法: python test_cookie.py
"""

import json
from pathlib import Path


def test_cookie():
    # ====================================================
    # 1. 读取 state.json
    # ====================================================
    account = "赤石嫣"
    state_file = Path(f"data/accounts/{account}/state.json")
    
    if not state_file.exists():
        print(f"❌ 文件不存在: {state_file}")
        print("请先用网页登录")
        return
    
    with open(state_file, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    # ====================================================
    # 2. 提取关键 Cookie
    # ====================================================
    cookie_dict = {}
    for cookie in data.get('cookies', []):
        name = cookie.get('name')
        if name in ['sessionid', 'uid_tt', 'sid_guard', 'ttwid', 'passport_csrf_token']:
            cookie_dict[name] = cookie.get('value')
    
    print("=" * 50)
    print("【提取的 Cookie】")
    for k, v in cookie_dict.items():
        print(f"  {k}: {v[:30]}..." if len(v) > 30 else f"  {k}: {v}")
    print("=" * 50)
    
    # ====================================================
    # 3. 检查关键字段
    # ====================================================
    if 'sessionid' not in cookie_dict:
        print("❌ 缺少 sessionid，登录态已失效")
        return
    
    cookie_str = '; '.join([f'{k}={v}' for k, v in cookie_dict.items()])
    
    # ====================================================
    # 4. 用 douyin-api 提取用户信息
    # ====================================================
    try:
        from douyin_api import DouYin
        print("✅ douyin-api 导入成功 (DouYin)")
    except ImportError:
        print("❌ douyin-api 未安装")
        print("请运行: pip install douyin-api")
        return
    
    try:
        print("🔄 正在请求抖音 API...")
        api = DouYin(cookie=cookie_str)
        user_info = api.get_current_user_info()
        
        print("=" * 50)
        print("【用户信息】")
        print(f"  昵称: {user_info.get('nickname', '未获取到')}")
        print(f"  抖音号: {user_info.get('unique_id', '未获取到')}")
        print(f"  sec_uid: {user_info.get('sec_uid', '未获取到')}")
        print("=" * 50)
        
        if user_info.get('nickname'):
            print("✅ 提取成功！")
        else:
            print("⚠️ 提取结果为空，可能 Cookie 已过期")
            
    except Exception as e:
        print(f"❌ 请求失败: {e}")


if __name__ == "__main__":
    test_cookie()
