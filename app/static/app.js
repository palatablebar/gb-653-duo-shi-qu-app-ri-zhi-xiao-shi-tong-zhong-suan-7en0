const state={busy:false};
async function callEndpoint(btn){
  if(state.busy)return; state.busy=true;
  const out=btn.parentElement.querySelector(".result"); out.textContent="运行中";
  try{
    const endpoint=btn.dataset.endpoint;
    const method=btn.dataset.method || (endpoint.includes("export")?"GET":"POST");
    const bodyRaw=btn.dataset.body;
    const init={method,headers:{"content-type":"application/json"}};
    if(method!=="GET") init.body=bodyRaw || "{}";
    const res=await fetch(endpoint,init);
    const type=res.headers.get("content-type")||"";
    out.textContent=type.includes("json")?JSON.stringify(await res.json(),null,2):await res.text();
  }catch(e){out.textContent="请求失败，请重试"}
  finally{state.busy=false}
}
document.querySelectorAll("[data-endpoint]").forEach(btn=>btn.addEventListener("click",()=>callEndpoint(btn)));
document.querySelector("#refresh")?.addEventListener("click",()=>location.reload());
