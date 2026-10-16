"""Small same-origin session console; Agent credentials never enter model inputs."""

CONSOLE = """<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>知证 · 团队检索服务</title>
<style>body{font:16px/1.7 system-ui;margin:7vh auto;padding:24px;max-width:820px;background:#14191b;color:#eee8dc}
input,button,textarea{font:inherit;padding:10px;margin:5px 0;border:1px solid #777;background:#20282c;color:inherit;border-radius:4px}
label{display:block}button{cursor:pointer}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#20282c;padding:20px}a{color:#d6b38a}</style>
<h1>知证 · 团队检索服务</h1><p>按知识库授权取得原文证据。此服务不调用模型，不开放旧版无认证接口。</p>
<form id="login"><label>用户名 <input name="username" autocomplete="username" required></label>
<label>密码 <input name="password" type="password" autocomplete="current-password" required></label><button>登录</button></form>
<section id="workspace" hidden><p id="identity"></p><label>知识库 <select id="kb"></select></label>
<label>检索问题 <textarea id="query" rows="3" cols="50" maxlength="4000"></textarea></label><button id="search">检索原文</button>
<button id="logout">退出</button><p>管理员通过受权HTTP API创建用户与授权；Agent通过用户限定的只读令牌调用HTTP接口。</p></section>
<pre id="result" role="status">请使用管理员建立的账号登录。</pre>
<script>
const result=document.getElementById('result');let csrf='';
async function api(path,options={}){const response=await fetch(path,{...options,headers:{'Content-Type':'application/json','X-CSRF-Token':csrf,...options.headers}});
 const data=await response.json();if(!response.ok)throw Error(data.error?.code||'请求失败');return data;}
async function refresh(){try{const me=await api('/v1/me');document.getElementById('identity').textContent=me.username;
 csrf=decodeURIComponent(document.cookie.split('; ').find(x=>x.startsWith('zw_csrf='))?.split('=')[1]||'');
 const bases=await api('/v1/workspaces/default/knowledge-bases');const select=document.getElementById('kb');select.replaceChildren();
 for(const base of bases.knowledge_bases){const option=document.createElement('option');option.value=base.id;option.textContent=base.title;select.append(option);}
 document.getElementById('login').hidden=true;document.getElementById('workspace').hidden=false;result.textContent='已登录。';}catch{}}
document.getElementById('login').onsubmit=async event=>{event.preventDefault();const form=new FormData(event.target);
 try{const data=await api('/v1/sessions',{method:'POST',body:JSON.stringify(Object.fromEntries(form))});csrf=data.csrf_token;event.target.password.value='';await refresh();}catch(error){result.textContent=error.message;}};
document.getElementById('search').onclick=async()=>{try{const data=await api('/v1/workspaces/default/retrievals',{method:'POST',headers:{'Idempotency-Key':'web-'+crypto.randomUUID()},
 body:JSON.stringify({query:document.getElementById('query').value,knowledge_base_ids:[document.getElementById('kb').value]})});result.textContent=JSON.stringify(data,null,2);}catch(error){result.textContent=error.message;}};
document.getElementById('logout').onclick=async()=>{await api('/v1/sessions/current',{method:'DELETE'});location.reload();};refresh();
</script></html>"""
