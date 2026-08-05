"""抓取当前屏幕的完整控件树 XML，保存到项目根目录 xml/page.xml。

校准 src/locators.py 的 xpath / content-desc 时用：手机停在目标页面后运行，
然后在 xml/page.xml 里查元素的 resource-id / content-desc / 层级路径。

运行：python tools/dump_hierarchy.py
"""

import os

import uiautomator2 as u2

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(PROJECT_ROOT, 'xml')
OUT_FILE = os.path.join(OUT_DIR, 'page.xml')


def main() -> None:
    # 1. 连接设备（如果是 USB 连接，直接 connect 即可）
    d = u2.connect()

    # 2. 抓取当前屏幕的完整 XML 结构
    xml_content = d.dump_hierarchy()

    # 3. 保存到 xml/ 目录
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(OUT_FILE, 'w', encoding='utf-8') as f:
        f.write(xml_content)

    print(f'抓取成功！已保存为 {OUT_FILE}')


if __name__ == '__main__':
    main()
