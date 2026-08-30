import { readFileSync } from "node:fs";
import { JSDOM } from "jsdom";
const html = readFileSync("k8si/ui/dashboard.html","utf8");
const FX = [{name:"solo",namespace:"default",pvc:"p",schedule:"0 2 * * *",paused:false,
  lastBackupResult:"success",lastBackupTime:"2026-08-30T10:00:00Z",nextBackupTime:null,
  lastBackupDuration:10,message:"",successRate:1,streak:1,recentRuns:[],lastRunRef:null}];
let phaseCb=null;
const dom = new JSDOM(html,{runScripts:"dangerously",url:"http://t.test/",pretendToBeVisual:true,
  beforeParse(w){
    w.fetch=(u)=>{const s=String(u);
      if(s.includes("/api/version"))return Promise.resolve({ok:true,status:200,json:()=>({version:"t"})});
      if(s.includes("/api/backups/"))return Promise.resolve({ok:true,status:200,json:()=>({runName:"solo-run-1"})});
      return Promise.resolve({ok:true,status:200,json:()=>Promise.resolve(FX)});};
    w.EventSource=class{constructor(u){phaseCb=this;} close(){}};
  }});
const {window}=dom,{document}=window;
await new Promise(r=>setTimeout(r,120));

// ---- PROOF 1: stale btn after render() ----
const btn=document.querySelector('button[aria-label="Backup now"]');
window.triggerBackup("default","solo",btn);
const fresh=document.querySelector('button[aria-label="Backup now"]');
console.log("1a) clicked btn still connected after render():", btn.isConnected);
console.log("1b) freshly rendered btn is the same node:", btn===fresh);
console.log("1c) fresh btn disabled?", fresh.disabled, "| classes:", JSON.stringify(fresh.className), "| title:", JSON.stringify(fresh.title));
console.log("1d) row badge shows:", document.querySelector("tr[data-name]").dataset.result);
await new Promise(r=>setTimeout(r,50));
// simulate SSE 'running' phase
phaseCb.onmessage({data:JSON.stringify({type:"phase",phase:"Running",time:"2026-08-30T10:01:00Z",message:"x"})});
console.log("1e) after SSE 'running': stale btn connected?",btn.isConnected,"| stale classes:",JSON.stringify(btn.className));
console.log("1f) LIVE button classes after SSE running:",JSON.stringify(document.querySelector('button[aria-label="Backup now"]').className));
phaseCb.onmessage({data:JSON.stringify({type:"done",result:"success"})});
console.log("1g) LIVE button title after done:",JSON.stringify(document.querySelector('button[aria-label="Backup now"]').title));

// ---- PROOF 2: openBackupLogs "No runs yet" wipes the SVG ----
const logsBtn=document.querySelector('button[aria-label="Logs"]');
console.log("2a) Logs btn has svg before:",!!logsBtn.querySelector("svg"));
window.openBackupLogs("default","solo");
console.log("2b) Logs btn label/text after:",JSON.stringify(logsBtn.textContent),"| has svg:",!!logsBtn.querySelector("svg"));
await new Promise(r=>setTimeout(r,3100));
console.log("2c) Logs btn after 3s timeout:",JSON.stringify(logsBtn.textContent),"| has svg:",!!logsBtn.querySelector("svg"));

// ---- PROOF 3: sorting survives a poll refresh? ----
document.querySelector('th[data-key="name"]').click();
console.log("3) sorted asc, chip:",JSON.stringify(document.getElementById("sort-chip").textContent));
