/* CDP Tracker - No dependencies. CDP.init({endpoint:'/api/v1/track'}); */
(function(w,d){
"use strict";
var CK_V="_cdp_vid",CK_S="_cdp_sid",CK_C="_cdp_consent",
    ST=18e5,VD=365,FI=2e3,MQ=100,
    cfg={endpoint:"/api/v1/track",autoPageView:1,trackClicks:1,trackScrollDepth:1,
      trackTimeOnPage:1,crossDomainDomains:[],cookieDomain:"",cookiePath:"/",
      cookieSecure:location.protocol==="https:",respectDNT:1},
    s={on:0,vid:null,sid:null,la:0,uid:null,tr:{},co:null,q:[],sq:[],pa:0,sd:0,utm:{}};

function id(){if(w.crypto&&w.crypto.randomUUID)return w.crypto.randomUUID();
  var t=Date.now();return"xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g,function(c){
  var r=(t+Math.random()*16)%16|0;t=Math.floor(t/16);return(c==="x"?r:(r&3|8)).toString(16)})}
function N(){return Date.now()}

function sC(n,v,dy){var p=[n+"="+encodeURIComponent(v),"path="+cfg.cookiePath];
  if(dy){var e=new Date;e.setTime(e.getTime()+dy*864e5);p.push("expires="+e.toUTCString())}
  if(cfg.cookieDomain)p.push("domain="+cfg.cookieDomain);
  if(cfg.cookieSecure)p.push("Secure");p.push("SameSite=Lax");d.cookie=p.join("; ")}
function gC(n){var m=d.cookie.match(new RegExp("(?:^|; )"+n.replace(/([.$?*|{}()[\]\\/+^])/g,"\\$1")+"=([^;]*)"));return m?decodeURIComponent(m[1]):null}

function iV(){var v=gC(CK_V);if(!v){v=id();sC(CK_V,v,VD)}s.vid=v}
function iS(){var i=gC(CK_S),ok=i&&s.la&&N()-s.la<ST;if(!ok)i=id();s.sid=i;s.la=N();sC(CK_S,i,0)}
function tc(){s.la=N();sC(CK_S,s.sid,0)}

function uUTM(){var r=w.location.search;if(!r)return;r.substring(1).split("&").forEach(function(x){
  var p=x.split("="),n=decodeURIComponent(p[0]);if(n.indexOf("utm_")===0&&p[1])s.utm[n]=decodeURIComponent(p[1])})}

function lCo(){var r=gC(CK_C);if(r)try{s.co=JSON.parse(r)}catch(e){}}
function sCo(c){s.co=c;sC(CK_C,JSON.stringify(c),VD)}
function ok(){return s.co!==null&&s.co.analytics===true}

function mk(t,n,p){return{event_id:id(),event_type:t,event_name:n||"",visitor_id:s.vid,
  session_id:s.sid,customer_id:s.uid,source:"web",timestamp:N()/1e3,properties:p||{},
  context:{url:location.href,path:location.pathname,title:d.title,referrer:d.referrer,
  ua:navigator.userAgent,utm:s.utm}}}

function snd(ev){if(!ev.length)return;var p=JSON.stringify({events:ev}),u=cfg.endpoint;
  if(navigator.sendBeacon&&navigator.sendBeacon(u,new Blob([p],{type:"application/json"})))return;
  try{var x=new XMLHttpRequest;x.open("POST",u,!0);x.setRequestHeader("Content-Type","application/json");x.send(p)}catch(e){}}
function enq(e){if(ok()){s.sq.push(e);if(s.sq.length>=MQ)fl()}else{s.q.push(e);if(s.q.length>MQ)s.q.shift()}}
function fl(){if(s.sq.length)snd(s.sq.splice(0,MQ))}
function dr(){while(s.q.length)s.sq.push(s.q.shift());fl()}

function bClk(){if(!cfg.trackClicks)return;d.addEventListener("click",function(e){
  var el=e.target,i=0;for(;i<5&&el&&el!==d;i++){if(el.tagName==="A"||el.tagName==="BUTTON")break;el=el.parentElement}
  if(!el||el.tagName!=="A"&&el.tagName!=="BUTTON")return;
  enq(mk("track","element_click",{tag:el.tagName,text:(el.innerText||"").substring(0,200),
    href:el.href||null,id:el.id||null}));tc()},!0)}

function bScr(){if(!cfg.trackScrollDepth)return;var tk=0;
  w.addEventListener("scroll",function(){if(tk)return;tk=1;requestAnimationFrame(function(){
  var dh=Math.max(d.body.scrollHeight,d.documentElement.scrollHeight),wh=w.innerHeight,
      st=w.pageYOffset||d.documentElement.scrollTop;
  var dp=dh>wh?Math.round((st+wh)/dh*100):100;
  if(dp>s.sd)s.sd=dp;tk=0})},{passive:!0})}

function bTop(){if(!cfg.trackTimeOnPage)return;s.pa=N();
  function lv(){var p=JSON.stringify({events:[mk("track","page_leave",
    {time_on_page:Math.round((N()-s.pa)/1e3),scroll_depth:s.sd})]});
    if(navigator.sendBeacon)navigator.sendBeacon(cfg.endpoint,new Blob([p],{type:"application/json"}))}
  d.addEventListener("visibilitychange",function(){if(d.visibilityState==="hidden")lv()});
  w.addEventListener("pagehide",lv)}

function bXD(){var m=cfg.crossDomainDomains;if(!m.length)return;
  d.addEventListener("click",function(e){var el=e.target;
    while(el&&el.tagName!=="A")el=el.parentElement;if(!el||!el.href)return;
    try{var u=new URL(el.href);
      for(var i=0;i<m.length;i++)if(u.hostname===m[i]||u.hostname.indexOf("."+m[i])>-1){
        u.searchParams.set("_cdp_vid",s.vid);u.searchParams.set("_cdp_sid",s.sid);
        el.href=u.toString();break}}catch(x){}},!0)}

function rXD(){try{var p=new URLSearchParams(w.location.search),v=p.get("_cdp_vid"),i=p.get("_cdp_sid");
  if(v){s.vid=v;sC(CK_V,v,VD)}if(i){s.sid=i;s.la=N();sC(CK_S,i,0)}}catch(e){}}

var CDP={
  init:function(o){if(s.on)return;if(cfg.respectDNT&&navigator.doNotTrack==="1"){s.on=1;return}
    if(o)for(var k in o)if(o.hasOwnProperty(k))cfg[k]=o[k];
    lCo();rXD();iV();iS();uUTM();bClk();bScr();bTop();bXD();
    setInterval(fl,FI);s.on=1;if(cfg.autoPageView)CDP.page()},
  track:function(n,p){if(s.on){tc();enq(mk("track",n,p||{}))}},
  page:function(p){if(!s.on)return;tc();s.sd=0;s.pa=N();
    var pr={url:location.href,path:location.pathname,title:d.title,referrer:d.referrer};
    if(p)for(var k in p)if(p.hasOwnProperty(k))pr[k]=p[k];enq(mk("page_view","page_view",pr))},
  identify:function(uid,t){if(!s.on)return;s.uid=uid;s.tr=t||{};tc();
    enq(mk("identify","identify",{user_id:uid,email:t&&t.email||null,phone:t&&t.phone||null,traits:t||{}}))},
  consent:function(c){var m={necessary:!0};if(c)for(var k in c)if(c.hasOwnProperty(k))m[k]=!!c[k];
    sCo(m);if(ok())dr()},
  revokeConsent:function(){sCo({necessary:!0,analytics:!1,marketing:!1,personalization:!1});s.q=[];s.sq=[]},
  getConsent:function(){return s.co},
  getVisitorId:function(){return s.vid},
  getSessionId:function(){return s.sid},
  flush:fl,
  reset:function(){sC(CK_V,"",-1);sC(CK_S,"",-1);s.vid=s.sid=s.uid=null;s.tr={};s.q=[];s.sq=[];iV();iS()}
};
w.CDP=CDP;
})(window,document);
