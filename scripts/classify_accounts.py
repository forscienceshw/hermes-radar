import asyncio
import json
import re
from pathlib import Path

from app.services.workbuddy_llm import call_workbuddy

BASE = Path(__file__).resolve().parents[1]
DATA = BASE / "data"
INPUT = DATA / "accounts_review.json"
PROGRESS = DATA / "account_classification_progress.json"
OUTPUT = DATA / "account_classification.json"

MODEL = "deepseek-v4.1-flash"
BATCH_SIZE = 60
CONCURRENCY = 4

CATEGORIES = {
    "LIFE_MOLECULAR": "生命科学基础（分子、细胞、遗传、发育、免疫、微生物、植物、动物、生态、进化）",
    "STRUCT_BIO": "结构生物学、蛋白结构预测与设计、冷冻电镜",
    "CHEM_BIOL": "化学生物学、药物化学、化学与生命交叉、分子探针",
    "SYNTH_BIO": "合成生物学、基因编辑、蛋白质工程、代谢工程",
    "OMICS_BIOINFO": "生物信息、组学、计算生物学、生物统计",
    "NEURO": "神经科学、脑科学、脑机接口",
    "BIOMED": "临床医学、疾病机制、病理、药学应用、公共卫生",
    "CHEM_MAT": "化学、化工、材料、高分子、催化、能源、纳米材料",
    "PHYS_MATH": "物理、数学、天文、地学、力学与基础理论",
    "CS_AI": "计算机、人工智能、算法、数据科学、软件",
    "ENG_TECH": "机械、电子、自动化、机器人、仪器、传感器、3D打印、微流控、制造",
    "INTERDISCIPLINARY": "无法归入单一学科、明确跨学科研究",
    "SCI_COMM": "期刊、出版社、综合科学媒体、科研资讯",
    "ACADEMIC_TOOL": "科研工具、文献管理、科研服务、实验外包、绘图与投稿服务",
    "CAMPUS_EDU": "校园生活、升学考试、中学教育、非科研培训",
    "SOCIAL_HUMANITIES": "人文社科、经济金融、法律、历史、心理、新闻传播、管理、艺术",
    "LIFE_ENTERTAINMENT": "动漫二次元、游戏、影视娱乐、生活方式、情感、体育、旅游、美食",
    "OTHER": "信息不足或多重属性无法判断",
}

SYSTEM = """你是严谨的科研公众号分类器。用户是临床医学本科生，未来转向生命科学基础研究并申请直博；关注所有自然科学，尤其重视生命科学基础、结构生物学、化学生物学、合成生物学、高分子化学、机械/微流控/仪器和交叉学科。她不需要社会科学、人文、动漫二次元、游戏、娱乐、普通校园生活和无关商业内容。
你只会收到公众号名称和最近文章标题。必须依据已有文字判断，不能编造公众号内容。个人号、课题组号、低频号可以是 OPTIONAL，不要因为名字像个人就排除。
请从给定类别中选择恰好一个 code，并给出 fit：
- CORE：自然科学科研核心，值得优先保留
- OPTIONAL：综合科普、临床医学、科研工具、跨学科外围或低频个人号，可保留但非最核心
- EXCLUDE：与自然科学科研无关
reason 用不超过18个汉字说明判断依据。不要输出类别定义外的词。
输出 JSON：{"results":[{"id":输入id,"code":"类别代码","fit":"CORE|OPTIONAL|EXCLUDE","reason":"理由"}]}。每个输入 id 必须恰好返回一次。"""


def rule_fallback(account: dict) -> dict:
    text = (account["name"] + " " + account.get("description", "")).lower()
    code = "OTHER"
    fit = "OPTIONAL"
    rules = [
        ("LIFE_ENTERTAINMENT", r"动漫|二次元|漫画|游戏|影视|追剧|综艺|娱乐|美食|旅游|情感|星座|穿搭|美妆|体育|小说|电影"),
        ("SOCIAL_HUMANITIES", r"经济|金融|股票|基金|法律|历史|哲学|社会学|心理|教育政策|新闻|传播|管理|职场|读书|文学|艺术|设计美学"),
        ("CAMPUS_EDU", r"中学|高中|考研|保研|高考|校园|校友|升学|留学|招生|辅导员|团委|学生会"),
        ("STRUCT_BIO", r"结构生物|cryo|冷冻电镜|蛋白结构|alphafold|结构预测|pdb|晶体学|结构解析"),
        ("SYNTH_BIO", r"合成生物|基因编辑|crispr|蛋白质工程|代谢工程|基因线路|合成菌群"),
        ("CHEM_BIOL", r"化学生物|药物化学|点击化学|生物正交|分子探针|化学生物学"),
        ("OMICS_BIOINFO", r"生信|生物信息|基因组|转录组|单细胞|多组学|组学|计算生物|gwas"),
        ("NEURO", r"神经|脑科学|脑机|突触|神经元|认知科学"),
        ("BIOMED", r"医学|临床|病理|疾病|肿瘤|癌症|医院|药學|药学|免疫治疗|诊断|护理|公卫|流行病"),
        ("LIFE_MOLECULAR", r"细胞|基因|蛋白|分子|生物|微生物|菌|植物|动物|生态|进化|遗传|发育|rna|dna|酶"),
        ("CHEM_MAT", r"化学|材料|高分子|催化|能源|电池|纳米|金属|有机|聚合物|水凝胶"),
        ("PHYS_MATH", r"物理|量子|光学|天文|宇宙|数学|力学|地学|地质|大气|气候"),
        ("CS_AI", r"人工智能|机器学习|深度学习|大模型|计算机|算法|数据科学|编程|代码"),
        ("ENG_TECH", r"机械|机器人|自动化|仪器|传感|微流控|3d打印|制造|电子|芯片|控制|工程"),
        ("ACADEMIC_TOOL", r"文献|投稿|科研服务|实验外包|测试|检测服务|绘图|科研工具|期刊查询|学术工具"),
        ("SCI_COMM", r"nature|science|cell press|期刊|出版社|学术资讯|科学媒体|科研圈|科普"),
    ]
    for candidate, pattern in rules:
        if re.search(pattern, text, re.I):
            code = candidate
            break
    if code in {"SOCIAL_HUMANITIES", "LIFE_ENTERTAINMENT", "CAMPUS_EDU"}:
        fit = "EXCLUDE"
    elif code in {"LIFE_MOLECULAR", "STRUCT_BIO", "CHEM_BIOL", "SYNTH_BIO", "OMICS_BIOINFO", "NEURO", "CHEM_MAT", "PHYS_MATH", "CS_AI", "ENG_TECH"}:
        fit = "CORE"
    elif code in {"BIOMED", "SCI_COMM", "ACADEMIC_TOOL", "INTERDISCIPLINARY"}:
        fit = "OPTIONAL"
    return {"id": account["id"], "code": code, "fit": fit, "reason": "规则初判"}


def parse_results(content: str) -> list[dict]:
    content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip(), flags=re.I)
    data = json.loads(content)
    return data.get("results") or data.get("items") or []


async def classify_batch(batch: list[dict], semaphore: asyncio.Semaphore) -> list[dict]:
    compact = [{"id": x["id"], "name": x["name"], "recent": (x.get("description") or "")[:120]} for x in batch]
    category_block = "\n".join(f"- {code} = {label}" for code, label in CATEGORIES.items())
    prompt = (f"可用类别代码表（code 必须严格使用左侧英文代码，不能把 CORE/OPTIONAL/EXCLUDE 填进 code）：\n{category_block}\n\n"
              "请分类以下公众号：\n" + json.dumps(compact, ensure_ascii=False))
    async with semaphore:
        for attempt in range(3):
            try:
                content = await call_workbuddy(
                    [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
                    MODEL, 300, 0.0,
                )
                results = parse_results(content)
                valid = {}
                for result in results:
                    category = str(result.get("code", ""))
                    fit = str(result.get("fit", "")).upper()
                    if category in CATEGORIES and fit in {"CORE", "OPTIONAL", "EXCLUDE"}:
                        valid[int(result["id"])] = {
                            "id": int(result["id"]), "code": category, "fit": fit,
                            "reason": str(result.get("reason", ""))[:40],
                        }
                if len(valid) >= int(len(batch) * 0.9):
                    for account in batch:
                        valid.setdefault(account["id"], rule_fallback(account))
                    return list(valid.values())
            except Exception as exc:
                print(f"batch retry {attempt + 1}: {exc}", flush=True)
                await asyncio.sleep(2)
        return [rule_fallback(account) for account in batch]


async def main():
    data = json.loads(INPUT.read_text(encoding="utf-8"))
    accounts = data["accounts"]
    progress = json.loads(PROGRESS.read_text(encoding="utf-8")) if PROGRESS.exists() else {}
    pending = [item for item in accounts if str(item["id"]) not in progress]
    batches = [pending[i:i + BATCH_SIZE] for i in range(0, len(pending), BATCH_SIZE)]
    print(f"accounts={len(accounts)} pending={len(pending)} batches={len(batches)}", flush=True)
    semaphore = asyncio.Semaphore(CONCURRENCY)
    completed = 0
    for start in range(0, len(batches), CONCURRENCY):
        group = batches[start:start + CONCURRENCY]
        grouped_results = await asyncio.gather(*(classify_batch(batch, semaphore) for batch in group))
        for batch, results in zip(group, grouped_results):
            for result in results:
                progress[str(result["id"])] = result
            completed += len(batch)
        PROGRESS.write_text(json.dumps(progress, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"classified {completed}/{len(pending)}", flush=True)
    for account in accounts:
        result = progress.get(str(account["id"])) or rule_fallback(account)
        account.update(result)
    OUTPUT.write_text(json.dumps({"categories": CATEGORIES, "accounts": accounts}, ensure_ascii=False, indent=2),
                      encoding="utf-8")
    print("DONE", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
