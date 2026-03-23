/* CDP Tracker - Lightweight, privacy-respecting client-side tracking. No dependencies.
   Usage: CDP.init({endpoint:'/api/v1/track'}); CDP.track('click',{id:'btn'}); */
(function(w,d){
"use strict";
var CK_VID="_cdp_vid",CK_SID="_cdp_sid",CK_CON="_cdp_consent",
    SESS_MS=18e5,VID_DAYS=365,FLUSH_MS=2e3,MAX_Q=100;

var cfg={endpoint:"/api/v1/track",autoPageView:true,trackClicks:true,
  trackScrollDepth:true,trackTimeOnPage:true,crossDomainDomains:[],
  cookieDomain:"",cookiePath:"/",cookieSecure:location.protocol==="https:",
  respectDNT:true,debug:false};

var s={on:false,vid:null,sid:null,sStart:0,last:0,uid:null,traits:{},
  consent:null,q:[],sq:[],pgAt:0,scrl:0,utm:{}},ft=null;

function uid(){
  if(w.crypto&&w.crypto.randomUUID)return w.crypto.randomUUID();
  var d=Date.now();
  return"xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g,function(c){
    var r=(d+Math.random()*16)%16|0;d=Math.floor(d/16);
    return(c==="x"?r:(r&0x3|0x8)).toString(16)});
}
function ts(){return Date.now()}
function log(){if(cfg.debug&&w.console){var a=[].slice.call(arguments);a.unshift("[CDP]");console.log.apply(console,a)}}

function setC(n,v,days){
  var p=[n+"="+encodeURIComponent(v),"path="+cfg.cookiePath];
  if(days){var e=new Date();e.setTime(e.getTime()+days*864e5);p.push("expires="+e.toUTCString())}
  if(cfg.cookieDomain)p.push("domain="+cfg.cookieDomain);
  if(cfg.cookieSecure)p.push("Secure");p.push("SameSite=Lax");
  d.cookie=p.join("; ");
}
function getC(n){var m=d.cookie.match(new RegExp("(?:^|; )"+n.replace(/([.$?*|{}()[\]\\/+^])/g,"\\$1")+"=([^;]*)"));return m?decodeURIComponent(m[1]):null}
function delC(n){setC(n,"",-1)}

function initVid(){var v=getC(CK_VID);if(!v){v=uid();setC(CK_VID,v,VID_DAYS);log("New visitor:",v)}s.vid=v;return v}
function initSid(){
  var id=getC(CK_SID),ok=id&&s.last&&(ts()-s.last<SESS_MS);
  if(!ok){id=uid();s.sStart=ts();log("New session:",id)}
  s.sid=id;s.last=ts();setC(CK_SID,id,0);return id;
}
function touch(){s.last=ts();setC(CK_SID,s.sid,0)}

function captureUTM(){
  var keys=["utm_source","utm_medium","utm_campaign","utm_term","utm_content"],
      sr=w.location.search;if(!sr)return;
  var qs=sr.substring(1).split("&");
  for(var i=0;i<qs.length;i++){var p=qs[i].split("="),k=decodeURIComponent(p[0]);
    if(keys.indexOf(k)!==-1&&p[1])s.utm[k]=decodeURIComponent(p[1])}
}

function loadCon(){var r=getC(CK_CON);if(r)try{s.consent=JSON.parse(r)}catch(e){}}
function saveCon(c){s.consent=c;setC(CK_CON,JSON.stringify(c),VID_DAYS)}
function hasCon(c){return s.consent?(c==="necessary"||!!s.consent[c]):false}
function canTrk(){return s.consent!==null&&hasCon("analytics")}

function mkEvt(t,n,p){
  return{event_id:uid(),event_type:t,event_name:n||"",visitor_id:s.vid,
    session_id:s.sid,customer_id:s.uid||null,source:"web",timestamp:ts()/1e3,
    properties:p||{},context:{page:{url:location.href,path:location.pathname,
    title:d.title,referrer:d.referrer},user_agent:navigator.userAgent,
    language:navigator.language||"",screen:{width:screen.width,height:screen.height},
    utm:s.utm,traits:s.traits}};
}

function send(evts){
  if(!evts.length)return;var pl=JSON.stringify({events:evts}),u=cfg.endpoint;
  if(navigator.sendBeacon){var b=new Blob([pl],{type:"application/json"});
    if(navigator.sendBeacon(u,b)){log("Sent",evts.length);return}}
  try{var x=new XMLHttpRequest();x.open("POST",u,true);
    x.setRequestHeader("Content-Type","application/json");x.send(pl)}catch(e){}
}

function enq(e){
  if(canTrk()){s.sq.push(e);if(s.sq.length>=MAX_Q)flush()}
  else{s.q.push(e);if(s.q.length>MAX_Q)s.q.shift()}
}
function flush(){if(!s.sq.length)return;send(s.sq.splice(0,MAX_Q))}
function drain(){while(s.q.length)s.sq.push(s.q.shift());flush()}

function bindClicks(){
  if(!cfg.trackClicks)return;
  d.addEventListener("click",function(e){
    var el=e.target,i=0;
    for(;i<5&&el&&el!==d;i++){if(el.tagName==="A"||el.tagName==="BUTTON")break;el=el.parentElement}
    if(!el||el.tagName!=="A"&&el.tagName!=="BUTTON")return;
    enq(mkEvt("track","element_click",{tag:el.tagName,text:(el.innerText||"").substring(0,255),
      href:el.href||null,id:el.id||null,classes:el.className||null}));touch()},true);
}

function bindScroll(){
  if(!cfg.trackScrollDepth)return;var tk=false;
  w.addEventListener("scroll",function(){if(tk)return;tk=true;
    requestAnimationFrame(function(){
      var dh=Math.max(d.body.scrollHeight,d.documentElement.scrollHeight),
          wh=w.innerHeight,st=w.pageYOffset||d.documentElement.scrollTop,
          dp=dh>wh?Math.round((st+wh)/dh*100):100;
      if(dp>s.scrl)s.scrl=dp;tk=false})},{passive:true});
}

function bindTimeOnPage(){
  if(!cfg.trackTimeOnPage)return;s.pgAt=ts();
  function leave(){
    var t=Math.round((ts()-s.pgAt)/1e3),
        pl=JSON.stringify({events:[mkEvt("track","page_leave",{time_on_page:t,scroll_depth:s.scrl})]});
    if(navigator.sendBeacon)navigator.sendBeacon(cfg.endpoint,new Blob([pl],{type:"application/json"}))
  }
  d.addEventListener("visibilitychange",function(){if(d.visibilityState==="hidden")leave()});
  w.addEventListener("pagehide",leave);
}

function bindCrossDomain(){
  if(!cfg.crossDomainDomains.length)return;
  d.addEventListener("click",function(e){
    var el=e.target;while(el&&el.tagName!=="A")el=el.parentElement;
    if(!el||!el.href)return;
    try{var u=new URL(el.href),m=false;
      for(var i=0;i<cfg.crossDomainDomains.length;i++){
        var dm=cfg.crossDomainDomains[i];
        if(u.hostname===dm||u.hostname.indexOf("."+dm)!==-1){m=true;break}}
      if(m){u.searchParams.set("_cdp_vid",s.vid);u.searchParams.set("_cdp_sid",s.sid);el.href=u.toString()}
    }catch(x){}},true);
}

function readXDomain(){
  var p=new URLSearchParams(w.location.search),v=p.get("_cdp_vid"),id=p.get("_cdp_sid");
  if(v){s.vid=v;setC(CK_VID,v,VID_DAYS)}
  if(id){s.sid=id;s.last=ts();setC(CK_SID,id,0)}
}

var CDP={
  init:function(o){
    if(s.on)return;
    if(cfg.respectDNT&&navigator.doNotTrack==="1"){s.on=true;return}
    if(o)for(var k in o)if(o.hasOwnProperty(k))cfg[k]=o[k];
    loadCon();readXDomain();initVid();initSid();captureUTM();
    bindClicks();bindScroll();bindTimeOnPage();bindCrossDomain();
    ft=setInterval(flush,FLUSH_MS);s.on=true;log("Init",cfg);
    if(cfg.autoPageView)CDP.page();
  },
  track:function(n,p){if(!s.on)return;touch();enq(mkEvt("track",n,p||{}))},
  page:function(p){
    if(!s.on)return;touch();s.scrl=0;s.pgAt=ts();
    var pr={url:location.href,path:location.pathname,title:d.title,referrer:d.referrer};
    if(p)for(var k in p)if(p.hasOwnProperty(k))pr[k]=p[k];
    enq(mkEvt("page_view","page_view",pr));
  },
  identify:function(id,t){
    if(!s.on)return;s.uid=id;s.traits=t||{};touch();
    enq(mkEvt("identify","identify",{user_id:id,email:t&&t.email||null,phone:t&&t.phone||null,traits:t||{}}));
  },
  consent:function(cats){
    var c={necessary:true};if(cats)for(var k in cats)if(cats.hasOwnProperty(k))c[k]=!!cats[k];
    saveCon(c);log("Consent:",c);if(canTrk())drain();
  },
  revokeConsent:function(){saveCon({necessary:true,analytics:false,marketing:false,personalization:false});s.q=[];s.sq=[]},
  getConsent:function(){return s.consent},
  getVisitorId:function(){return s.vid},
  getSessionId:function(){return s.sid},
  flush:flush,
  reset:function(){delC(CK_VID);delC(CK_SID);s.vid=s.sid=s.uid=null;s.traits={};s.q=[];s.sq=[];initVid();initSid()}
};
w.CDP=CDP;
})(window,document);
