import React,{useEffect,useState} from 'react';
import {Download,Film,LoaderCircle,RefreshCw,ArrowUpRight,Check} from 'lucide-react';
export default function Preview({creation, onEdit, onRecover, submitting}){
 const [selected,setSelected]=useState(0),[prompt,setPrompt]=useState('');
 useEffect(()=>{setSelected(Math.max(0,(creation?.versions.length||1)-1));setPrompt('')},[creation?.id,creation?.versions.length]);
 const versions=creation?.versions||[],version=versions[selected];
 const pending=['queued','running','pending','unknown'].includes(versions.at(-1)?.status);
 const names={queued:'等待开始',running:'正在生成',pending:'等待结果',unknown:'需要确认',ready:'已就绪',failed:'未完成'};
 return <section className="panel preview" aria-label="视频预览"><div className="preview-heading"><h2>{creation?.title||'把想法变成画面'}</h2>{version&&<span className={'status '+version.status}>{version.status==='ready'?<Check size={16}/>:null}{names[version.status]}</span>}</div>
 <div className={'screen '+(!version?.url?'empty':'')}>
 {version?.url?<video key={version.url} src={version.url} controls playsInline preload="metadata" aria-label={'版本 '+(selected+1)+' 视频'}/>:<div className="empty-content">{creation?.busy?<LoaderCircle className="spinner" size={34}/>:<Film size={34} strokeWidth={1.3}/>}<h3>{creation?names[version?.status]||'准备中':'你的下一支视频，从这里开始'}</h3><p>{creation?version?.message:'选几张参考图，写下想拍的画面。生成后，在这里预览和修改。'}</p></div>}
 </div>
 {creation&&<><div className="version-row"><div className="versions" role="tablist" aria-label="视频版本">{versions.map((v,i)=><button key={i} role="tab" aria-selected={i===selected} className={selected===i?'selected':''} onClick={()=>setSelected(i)}>版本 {i+1}{v.status!=='ready'&&<span className="version-dot"/>}</button>)}</div>{version?.url&&<a className="button download" href={version.url+'?download=true'} download><Download size={16}/>下载视频</a>}</div>
 <p className="version-caption">{version?.parent!=null?`基于版本 ${version.parent+1} 修改 · `:''}{version?.prompt}</p>
 {(pending||versions.at(-1)?.status==='failed')&&<div className="notice" role="status"><span>{versions.at(-1)?.message}{versions.at(-1)?.status==='pending'&&' 页面会继续定时查询原任务。'}</span>{pending&&!creation.busy&&<button className="button" disabled={submitting} onClick={onRecover}><RefreshCw size={15}/>立即查询</button>}</div>}
 <form className="edit-form" onSubmit={async e=>{e.preventDefault();if(await onEdit(prompt,selected))setPrompt('')}}><label className="visually-hidden" htmlFor="edit">修改描述</label><input id="edit" value={prompt} onChange={e=>setPrompt(e.target.value)} maxLength={2000} placeholder="想改哪里？例如：把桌面改成浅木色" disabled={!version?.url||pending}/><button className="primary" disabled={!version?.url||pending||submitting||!prompt.trim()}>修改视频<ArrowUpRight size={17}/></button></form><p className="hint">修改会生成新版本，保留原视频；每次修改调用一次视频 API。</p></>}
 {!creation&&<div className="empty-footer"><span>01 写下创意</span><span>02 生成预览</span><span>03 修改与下载</span></div>}
 </section>
}
