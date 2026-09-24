import React,{useCallback,useEffect,useState,useRef} from 'react';
import {createRoot} from 'react-dom/client';
import {Clapperboard,Plus,X,Film} from 'lucide-react';
import {api,post} from './api';
import Composer from './Composer';
import Preview from './Preview';
import './style.css';
function App(){
 const [list,setList]=useState([]),[selected,setSelected]=useState(null),[submitting,setSubmitting]=useState(false),[error,setError]=useState(''),[loaded,setLoaded]=useState(false);
 const refresh=useCallback(async()=>{try{const data=await api('/creations');setList(data);setLoaded(true)}catch(e){setError(e.message)}},[]);
 useEffect(()=>{refresh();const timer=setInterval(refresh,3000);return()=>clearInterval(timer)},[refresh]);
 const creation=list.find(c=>c.id===selected);
 const waitingForProvider=creation?.versions.at(-1)?.status==='pending'&&!creation.busy;
 useEffect(()=>{
  if(!selected||!waitingForProvider)return;
  const timer=setTimeout(async()=>{
   try{await post('/creations/'+selected+'/recover',{})}
   catch(e){if(!e.message.includes('正在处理'))setError('自动查询失败：'+e.message)}
   await refresh();
  },30000);
  return()=>clearTimeout(timer);
 },[selected,waitingForProvider,creation?.updated_at,refresh]);
 async function action(fn){setSubmitting(true);setError('');try{await fn();await refresh();return true}catch(e){setError(e.message);return false}finally{setSubmitting(false)}}
 const createRequest=useRef(null);
 const create=(form,files)=>action(async()=>{const fingerprint=JSON.stringify([form,files.map(f=>[f.name,f.size,f.lastModified])]);if(createRequest.current?.fingerprint!==fingerprint)createRequest.current={fingerprint,id:crypto.randomUUID().replaceAll('-','')};const body=new FormData();body.append('options',JSON.stringify({...form,request_id:createRequest.current.id}));files.forEach(f=>body.append('images',f));const data=await api('/creations',{method:'POST',body});setSelected(data.id);createRequest.current=null});
 return <><header><a className="brand" href="/" aria-label="Omni Studio 首页"><Clapperboard size={23}/><span>Omni Studio</span></a><span className="local-label"><i/>本地工作台</span></header><main>
 {error&&<div className="error" role="alert"><span>{error}</span><button aria-label="关闭提示" onClick={()=>setError('')}><X size={18}/></button></div>}
 <div className="workspace"><Composer onCreate={create} submitting={submitting} onError={setError}/><Preview creation={creation} submitting={submitting} onRecover={()=>action(()=>post('/creations/'+selected+'/recover',{}))} onEdit={(prompt,version)=>action(()=>post('/creations/'+selected+'/edit',{prompt,version}))}/></div>
 <section className="panel history"><div className="history-heading"><h2>最近创作</h2><button className="text-button" onClick={()=>setSelected(null)}><Plus size={16}/>新的创作</button></div>
 {!list.length?<p className="history-empty">{loaded?'还没有作品。你的创意、素材和版本会保存在本机。':'正在读取本地作品…'}</p>:<div className="history-list">{list.map(c=><button className={'history-item '+(c.id===selected?'active':'')} key={c.id} onClick={()=>setSelected(c.id)}>{c.references[0]?<img src={c.references[0]} alt=""/>:<span className="history-icon"><Film size={22}/></span>}<span><strong>{c.title}</strong><small>{c.options.duration} 秒 · {c.versions.length} 个版本{c.busy?' · 处理中':''}</small></span></button>)}</div>}
 </section><footer>创意留在你手中，作品保存在本机。生成时，创意与所选素材会发送给已配置的视频模型。</footer></main></>
}
createRoot(document.getElementById('root')).render(<App/>);
