import csv
import html
import json
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
DATA = BASE / "data"
SOURCE = DATA / "account_classification.json"
HTML_OUT = DATA / "公众号分类清单.html"
CSV_OUT = DATA / "公众号分类清单.csv"

LABELS = {
    "LIFE_MOLECULAR": "生命科学基础",
    "STRUCT_BIO": "结构生物学",
    "CHEM_BIOL": "化学生物学",
    "SYNTH_BIO": "合成生物学",
    "OMICS_BIOINFO": "生信与组学",
    "NEURO": "神经科学",
    "BIOMED": "生物医学临床",
    "CHEM_MAT": "化学与材料",
    "PHYS_MATH": "物理数学天文",
    "CS_AI": "计算机与AI",
    "ENG_TECH": "工程与微流控",
    "INTERDISCIPLINARY": "跨学科",
    "SCI_COMM": "期刊与科学媒体",
    "ACADEMIC_TOOL": "科研工具服务",
    "CAMPUS_EDU": "校园教育",
    "SOCIAL_HUMANITIES": "人文社科",
    "LIFE_ENTERTAINMENT": "生活娱乐动漫",
    "OTHER": "待判断",
}

HTML = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>公众号分类筛选</title>
<style>
:root{--bg:#f4f6f8;--panel:#fff;--ink:#18212b;--muted:#66717e;--line:#dfe5ea;--accent:#126e67;--soft:#e9f4f2}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.55 system-ui,"Microsoft YaHei",sans-serif}
header{background:#fff;border-bottom:1px solid var(--line);padding:20px 28px;position:sticky;top:0;z-index:5}
h1{margin:0 0 4px;font-size:22px}.muted{color:var(--muted)}
.summary{display:flex;gap:8px;flex-wrap:wrap;margin:14px 0}.chip{padding:5px 10px;border-radius:20px;background:#eef2f4;font-size:12px}
.chip.core{background:#e1f3ea;color:#147251}.chip.optional{background:#fff2d6;color:#805c0c}.chip.exclude{background:#fde8e6;color:#b42318}
.toolbar{display:flex;gap:8px;flex-wrap:wrap;align-items:center}input,select,button{font:inherit;border:1px solid #cbd5dc;border-radius:7px;padding:7px 10px;background:#fff;color:var(--ink)}
input[type=search]{min-width:300px}button{cursor:pointer}.primary{background:var(--accent);color:#fff;border-color:var(--accent)}
.stats{font-weight:600;color:var(--accent)}main{padding:18px 28px 80px}.table{background:#fff;border:1px solid var(--line);border-radius:10px;overflow:auto;max-height:calc(100vh - 230px)}
table{border-collapse:collapse;width:100%;min-width:1100px}th,td{padding:9px 11px;border-bottom:1px solid #edf0f2;text-align:left;vertical-align:top}th{position:sticky;top:0;background:#f7f9fa;z-index:2;white-space:nowrap}
tr:hover td{background:#fbfcfc}.name{font-weight:650}.recent{color:var(--muted);max-width:500px}.reason{color:#596573;max-width:220px}.badge{display:inline-block;padding:2px 7px;border-radius:14px;background:#eef2f4;white-space:nowrap}
.core{color:#147251;background:#e1f3ea}.optional{color:#805c0c;background:#fff2d6}.exclude{color:#b42318;background:#fde8e6}.update{font-size:12px;color:#7a8591}
footer{position:fixed;bottom:0;left:0;right:0;background:#fff;border-top:1px solid var(--line);padding:10px 24px;display:flex;justify-content:space-between;align-items:center;gap:10px}.caveat{background:#fff8e8;border:1px solid #ead49a;color:#624d1c;padding:10px 13px;border-radius:8px;margin-bottom:14px}
</style></head><body>
<header><h1>公众号分类筛选</h1><div class="muted">按研究方向粗筛，勾选后复制名称，用于科研雷达的批量公众号导入。默认只勾选 CORE。</div>
<div id="summary" class="summary"></div>
<div class="toolbar"><input id="q" type="search" placeholder="搜索名称、简介、分类或理由"><select id="category"><option value="">全部分类</option></select><select id="fit"><option value="">全部建议</option><option value="CORE">CORE 优先保留</option><option value="OPTIONAL">OPTIONAL 可选</option><option value="EXCLUDE">EXCLUDE 排除</option></select><button id="checkVisible">勾选当前结果</button><button id="uncheckVisible">取消当前结果</button><button id="reset">恢复默认</button></div></header>
<main><div class="caveat">OCR 来自截图，个别名称可能有 1–2 个字符识别错误。正式导入前可搜索核对；建议先选 CORE，再从 OPTIONAL 补充。</div>
<div class="table"><table><thead><tr><th>保留</th><th>公众号</th><th>建议</th><th>分类</th><th>理由</th><th>最近文章</th></tr></thead><tbody id="rows"></tbody></table></div></main>
<footer><span>已选 <b id="selectedCount">0</b> 个 / 当前显示 <b id="visibleCount">0</b> 个</span><div class="toolbar"><button id="copy">复制已选名称</button><button id="export" class="primary">导出已选 CSV</button></div></footer>
<script>
const ROWS=__DATA__;const CATS=__CATEGORIES__;const LABELS=__LABELS__;
const tbody=document.getElementById('rows'),q=document.getElementById('q'),cat=document.getElementById('category'),fit=document.getElementById('fit');
for(const [k,v] of Object.entries(LABELS)){const o=document.createElement('option');o.value=k;o.textContent=v;cat.appendChild(o)}
const saved=JSON.parse(localStorage.getItem('radar-account-selection')||'{}');const selected=new Set(ROWS.filter(r=>saved[r.id]===true||(saved[r.id]===undefined&&r.fit==='CORE')).map(r=>r.id));
function isStale(v){return /年|月|天前|周前/.test(v||'')}
function render(){const query=q.value.trim().toLowerCase();tbody.innerHTML='';let visible=0;
 for(const r of ROWS){if(query&&!(r.name+' '+r.description+' '+r.reason+' '+LABELS[r.code]).toLowerCase().includes(query))continue;if(cat.value&&r.code!==cat.value)continue;if(fit.value&&r.fit!==fit.value)continue;visible++;
  const tr=document.createElement('tr');tr.innerHTML=`<td><input type="checkbox" data-id="${r.id}" ${selected.has(r.id)?'checked':''}></td><td><div class="name">${esc(r.name)}</div><div class="update">#${r.id} · OCR ${r.confidence}</div></td><td><span class="badge ${r.fit.toLowerCase()}">${r.fit}</span></td><td><span class="badge">${esc(LABELS[r.code])}</span></td><td class="reason">${esc(r.reason||'')}</td><td class="recent">${esc(r.description||'')}</td>`;tbody.appendChild(tr)}
 document.getElementById('visibleCount').textContent=visible;document.getElementById('selectedCount').textContent=selected.size;save()}
function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function save(){const obj={};for(const r of ROWS)obj[r.id]=selected.has(r.id);localStorage.setItem('radar-account-selection',JSON.stringify(obj))}
function summary(){const c={CORE:0,OPTIONAL:0,EXCLUDE:0};for(const r of ROWS)c[r.fit]++;document.getElementById('summary').innerHTML=`<span class="chip core">CORE ${c.CORE}</span><span class="chip optional">OPTIONAL ${c.OPTIONAL}</span><span class="chip exclude">EXCLUDE ${c.EXCLUDE}</span><span class="chip">总计 ${ROWS.length}</span>`}
tbody.addEventListener('change',e=>{if(e.target.matches('input[data-id]')){const id=+e.target.dataset.id;e.target.checked?selected.add(id):selected.delete(id);document.getElementById('selectedCount').textContent=selected.size;save()}});
for(const el of [q,cat,fit])el.addEventListener('input',render);
document.getElementById('checkVisible').onclick=()=>{document.querySelectorAll('#rows input[data-id]').forEach(x=>{x.checked=true;selected.add(+x.dataset.id)});document.getElementById('selectedCount').textContent=selected.size;save()};
document.getElementById('uncheckVisible').onclick=()=>{document.querySelectorAll('#rows input[data-id]').forEach(x=>{x.checked=false;selected.delete(+x.dataset.id)});document.getElementById('selectedCount').textContent=selected.size;save()};
document.getElementById('reset').onclick=()=>{selected.clear();for(const r of ROWS)if(r.fit==='CORE')selected.add(r.id);render()};
document.getElementById('copy').onclick=async()=>{const text=ROWS.filter(r=>selected.has(r.id)).map(r=>r.name).join('\n');try{await navigator.clipboard.writeText(text)}catch(e){const t=document.createElement('textarea');t.value=text;document.body.appendChild(t);t.select();document.execCommand('copy');t.remove()}alert('已复制 '+selected.size+' 个公众号名称')};
document.getElementById('export').onclick=()=>{const rows=ROWS.filter(r=>selected.has(r.id));const head=['名称','分类','建议','理由','最近更新','最近文章'];const lines=[head,...rows.map(r=>[r.name,LABELS[r.code],r.fit,r.reason,r.last_update,r.description])];const csv='\ufeff'+lines.map(line=>line.map(v=>'"'+String(v??'').replace(/"/g,'""')+'"').join(',')).join('\r\n');const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([csv],{type:'text/csv'}));a.download='公众号_已选清单.csv';a.click();URL.revokeObjectURL(a.href)};
summary();render();
</script></body></html>"""


def main():
    data = json.loads(SOURCE.read_text(encoding="utf-8"))
    accounts = data["accounts"]
    serialized = json.dumps(accounts, ensure_ascii=False).replace("</", "<\\/")
    page = (HTML.replace("__DATA__", serialized)
                .replace("__CATEGORIES__", json.dumps(data["categories"], ensure_ascii=False))
                .replace("__LABELS__", json.dumps(LABELS, ensure_ascii=False)))
    HTML_OUT.write_text(page, encoding="utf-8")
    with CSV_OUT.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["序号", "公众号名称", "建议分类", "建议保留", "分类理由", "最近更新", "最近文章/简介", "OCR置信度"])
        for account in sorted(accounts, key=lambda x: (x["fit"] != "CORE", x["code"], x["name"])):
            writer.writerow([account["id"], account["name"], LABELS.get(account["code"], account["code"]),
                             account["fit"], account["reason"], account.get("last_update", ""),
                             account.get("description", ""), account["confidence"]])
    print(HTML_OUT)
    print(CSV_OUT)


if __name__ == "__main__":
    main()
