"""测试 ContextCompressor 压缩效果"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from baidu_search.compressor import ContextCompressor

query = "Agent 强化学习 RL 例子 讲解 简单入门"
text = '''强化学习里的 Agent 其实就是那个“决策者”，它通过不断试错来学会怎么最大化累计奖励。昨天我同事又在群里发他家猫的视频，太懒了根本不想动。最经典的入门例子就是 CartPole（小车倒立摆），环境是一个小车，上面竖着一根杆子，Agent 的任务是通过左右推小车，让杆子一直保持竖直不倒。每次杆子越接近垂直就给正奖励，倒了就结束并给大负奖励。我前天点了个外卖，结果骑手迷路绕了三圈。Agent 会从随机乱动开始（比如用 ε-greedy 策略），慢慢通过 Q-learning 或者 Policy Gradient 学到：当杆子向右倾斜时就往右推，倾斜左就往左推，这样才能平衡。说起来我冰箱里好像没牛奶了，晚上得去买。另一个超级简单的例子是 网格世界（Grid World），比如 4×5 的格子，Agent 从左下角出发，目标是右上角拿金币，中间有几个陷阱格子掉进去就-100分。Agent 每走一步消耗-1分，所以它会尽量走最短路径避开陷阱。昨天看直播有人抽卡十连全歪，血压拉满。在代码实现上，通常先定义环境（用 gym 库的 CartPole-v1 就行），然后初始化一个 Agent（比如表格型的 Q-table，或者神经网络做 approximator），再跑很多 episode 让它跟环境交互、收集 (s, a, r, s') 元组更新策略。顺便提一句，我最近迷上喝冰美式，加了点椰奶意外好喝。强化学习的精髓就是没有老师告诉你正确答案，只有稀疏的奖励信号，Agent 全靠自己摸索规律。去年双十一我买了个空气炸锅，用到现在都没坏。常见算法里，DQN 适合离散动作空间，像玩 Atari 游戏；PPO 现在特别火，因为稳定又 sample-efficient，适合连续控制任务比如机器人走路。哦对了，我 Switch 上新买的游戏还没拆封，什么时候有空开一把。总之想入门 RL Agent，先从 OpenAI Gym 的 CartPole 开始写一个 Q-learning 或 REINFORCE，跑个几百上千 episode 就能看到小车学会平衡了，超级有成就感！对了，你平时用什么输入法？我最近换成搜狗了，手感还行。'''
print(f"原文长度: {len(text)}")
print("=" * 60)

for max_chars in [500, 1000, 2000]:
    comp = ContextCompressor(max_chars=max_chars, splitter="simple")
    # comp = ContextCompressor(max_chars=max_chars, splitter="jina")
    result = comp.compress(query, text)
    print(f"\n[max_chars={max_chars}] 压缩后长度: {len(result)}")
    print(f"压缩率: {len(result)/len(text):.1%}")
    # print(f"内容预览: {result[:200]}...")
    print(f"内容预览: {result}...")
    print("-" * 60)

# 搜索“空气炸锅”是否还在500chars的段落里。 实测简单句子拆分的就不错了。