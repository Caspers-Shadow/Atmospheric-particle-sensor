const componentInfo = {
  pi:["Raspberry Pi 5","The central controller communicates with the sensors, processes readings, updates the local display and sends selected values to ThingSpeak."],
  bme:["BME280","Measures temperature, relative humidity and barometric pressure. In the reported ground tests, the archived program did not apply CPU-temperature compensation."],
  pm:["PMS5003","A fan-assisted optical particulate sensor reporting cumulative PM1.0, PM2.5 and PM10 mass-concentration fractions."],
  gas:["MICS6814","Provides oxidising, reducing and ammonia-related resistance channels. In this project they are used as raw or relative indicators, not calibrated gas concentrations."],
  ltr:["LTR559","Measures ambient light and proximity. Proximity is also used as a physical interaction input to move between display modes."],
  lcd:["ST7735 LCD","Shows current measurements and short histories locally so the artefact can be explored without needing a separate computer."]
};

document.querySelectorAll(".component").forEach(btn=>{
  btn.addEventListener("click",()=>{
    document.querySelectorAll(".component").forEach(x=>x.classList.remove("active"));
    btn.classList.add("active");
    const [title,body]=componentInfo[btn.dataset.component];
    document.getElementById("componentDetail").innerHTML=`<p class="detail-kicker">${title}</p><p>${body}</p>`;
  });
});

const observer = new IntersectionObserver(entries=>{
  entries.forEach(entry=>{ if(entry.isIntersecting) entry.target.classList.add("visible"); });
},{threshold:.14});
document.querySelectorAll(".reveal").forEach(el=>observer.observe(el));

const sky=document.getElementById("sky");
const sctx=sky.getContext("2d");
let stars=[];
function resizeSky(){
  const dpr=Math.min(devicePixelRatio||1,2);
  sky.width=innerWidth*dpr;sky.height=innerHeight*dpr;
  sky.style.width=innerWidth+"px";sky.style.height=innerHeight+"px";
  sctx.setTransform(dpr,0,0,dpr,0,0);
  stars=Array.from({length:Math.min(180,Math.floor(innerWidth/7))},()=>({
    x:Math.random()*innerWidth,y:Math.random()*innerHeight,r:Math.random()*1.3+.2,a:Math.random()*.45+.1
  }));
}
function drawSky(){
  sctx.clearRect(0,0,innerWidth,innerHeight);
  const shift=scrollY*.025;
  stars.forEach(st=>{
    let y=(st.y-shift)%innerHeight;if(y<0)y+=innerHeight;
    sctx.beginPath();sctx.arc(st.x,y,st.r,0,Math.PI*2);
    sctx.fillStyle=`rgba(180,224,255,${st.a})`;sctx.fill();
  });
  requestAnimationFrame(drawSky);
}
resizeSky();addEventListener("resize",resizeSky);drawSky();

let rows=[], currentField="temperature";
const labels={temperature:["Temperature","°C"],pressure:["Pressure","hPa"],pm25:["PM2.5","µg/m³"],humidity:["Humidity","%"]};

fetch("../experiment.csv")
  .then(r=>{if(!r.ok)throw new Error("CSV not found");return r.text();})
  .then(text=>{
    const lines=text.trim().split(/\r?\n/);
    const headers=lines[0].split(",");
    rows=lines.slice(1).map(line=>{
      const values=line.split(",");
      const obj={};headers.forEach((h,i)=>obj[h]=values[i]);
      return obj;
    }).filter(r=>r.created_at);
    drawChart();
  })
  .catch(()=>{
    document.getElementById("chartCaption").textContent="Repository CSV could not be loaded in this preview. The rest of the site remains fully usable.";
  });

document.querySelectorAll(".chart-tab").forEach(tab=>{
  tab.addEventListener("click",()=>{
    document.querySelectorAll(".chart-tab").forEach(x=>x.classList.remove("active"));
    tab.classList.add("active");currentField=tab.dataset.field;drawChart();
  });
});

function drawChart(){
  if(!rows.length)return;
  const canvas=document.getElementById("dataChart");
  const ctx=canvas.getContext("2d");
  const dpr=Math.min(devicePixelRatio||1,2);
  const w=canvas.clientWidth,h=canvas.clientHeight;
  canvas.width=w*dpr;canvas.height=h*dpr;ctx.setTransform(dpr,0,0,dpr,0,0);
  ctx.clearRect(0,0,w,h);
  const pad={l:54,r:18,t:20,b:42};
  const vals=rows.map(r=>Number(r[currentField])).filter(Number.isFinite);
  if(!vals.length)return;
  let min=Math.min(...vals),max=Math.max(...vals);
  if(min===max){min-=1;max+=1}
  const spread=max-min;min-=spread*.08;max+=spread*.08;
  ctx.strokeStyle="rgba(183,222,255,.12)";ctx.lineWidth=1;
  for(let i=0;i<5;i++){
    const y=pad.t+(h-pad.t-pad.b)*(i/4);
    ctx.beginPath();ctx.moveTo(pad.l,y);ctx.lineTo(w-pad.r,y);ctx.stroke();
    const v=max-(max-min)*(i/4);
    ctx.fillStyle="rgba(159,183,204,.72)";ctx.font="11px ui-monospace, monospace";ctx.fillText(v.toFixed(1),4,y+4);
  }
  ctx.beginPath();
  rows.forEach((r,i)=>{
    const value=Number(r[currentField]);if(!Number.isFinite(value))return;
    const x=pad.l+(w-pad.l-pad.r)*(i/Math.max(rows.length-1,1));
    const y=pad.t+(h-pad.t-pad.b)*(1-(value-min)/(max-min));
    i===0?ctx.moveTo(x,y):ctx.lineTo(x,y);
  });
  const g=ctx.createLinearGradient(pad.l,0,w-pad.r,0);g.addColorStop(0,"#61e7ff");g.addColorStop(1,"#9c7cff");
  ctx.strokeStyle=g;ctx.lineWidth=2;ctx.stroke();
  const [name,unit]=labels[currentField];
  const first=new Date(rows[0].created_at),last=new Date(rows[rows.length-1].created_at);
  document.getElementById("chartCaption").textContent=`${name} · ${rows.length} repository sample rows · ${min.toFixed(1)}–${max.toFixed(1)} ${unit} · ${first.toLocaleTimeString([], {hour:"2-digit",minute:"2-digit"})} to ${last.toLocaleTimeString([], {hour:"2-digit",minute:"2-digit"})}`;
}
addEventListener("resize",()=>{if(rows.length)drawChart()});
