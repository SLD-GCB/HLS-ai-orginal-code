"""整轮验证：用包内那套环境，把用户失败的那次构建原样跑一遍（pip wheel）。"""
import os
import sys
import time

根 = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..'))
sys.path.insert(0, 根)
import 酒馆编译                                   # noqa: E402

日志 = open(os.path.join(根, '构建缓存', '_构建验证日志.txt'), 'w', encoding='utf-8')

def 收(行):
    日志.write(行 + '\n')
    日志.flush()

os.environ['酒馆_编译环境'] = os.path.join(根, 'dist', '编译环境')
收('真实包路径 : %s' % os.environ['酒馆_编译环境'])
收('环境目录   : %s' % 酒馆编译.环境目录())
收('齐不齐     : %s' % (酒馆编译.齐不齐(),))
起 = time.time()
try:
    wheel, _ = 酒馆编译.编译('120', 行回调=收)
    收('')
    收('===== 成功 =====  用时 %.0f 分' % ((time.time() - 起) / 60))
    收('产物: %s（%s 字节）' % (wheel, os.path.getsize(wheel) if wheel and os.path.isfile(wheel) else '??'))
except Exception as 错:
    收('')
    收('===== 失败 =====  用时 %.0f 分' % ((time.time() - 起) / 60))
    收('%s: %s' % (type(错).__name__, 错))
finally:
    日志.close()
