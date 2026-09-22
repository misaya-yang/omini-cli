import React, {useEffect, useRef, useState} from 'react';
import {Plus, X, ArrowUpRight, ImagePlus} from 'lucide-react';
const initial = {brief:'',duration:4,aspect:'16:9',resolution:'360p',image_mode:'reference',provenance:'owned'};
export default function Composer({onCreate, submitting, onError}) {
  const [form,setForm] = useState(()=>{try{return {...initial,...JSON.parse(localStorage.getItem('omni-draft')||'{}')}}catch{return initial}});
  const [files,setFiles] = useState([]), [drag,setDrag] = useState(false);
  const input = useRef(null);
  useEffect(()=>{try{localStorage.setItem('omni-draft',JSON.stringify(form))}catch{}},[form]);
  useEffect(()=>()=>files.forEach(f=>URL.revokeObjectURL(f.url)),[files]);
  function update(key,value){setForm(f=>({...f,[key]:value}));}
  function add(incoming){
    const allowed=[...incoming];
    if(files.length+allowed.length>5){onError('最多选择 5 张图片。');return;}
    if(allowed.some(f=>!['image/jpeg','image/png','image/webp'].includes(f.type)||f.size>10*1024*1024)){onError('请选择 10 MB 以内的 JPG、PNG 或 WebP 图片。');return;}
    // Recreate object URLs because the previous effect cleans up the prior list.
    setFiles(old=>[...old.map(f=>({file:f.file,url:URL.createObjectURL(f.file)})),...allowed.map(file=>({file,url:URL.createObjectURL(file)}))]);
  }
  return <aside className="panel composer"><h2>开始创作</h2><form onSubmit={e=>{e.preventDefault();onCreate(form,files.map(f=>f.file));}}>
    <label htmlFor="brief">你想拍什么？</label><div className="brief-wrap"><textarea id="brief" required maxLength={4000} value={form.brief} onChange={e=>update('brief',e.target.value)} placeholder="例如：午后的阳光洒在桌上，镜头缓缓推进，窗帘随风轻轻摆动。"/><span>{form.brief.length}/4000</span></div>
    <div className="label-row"><label htmlFor="images">参考图片 <small>可选</small></label><span>{files.length}/5</span></div>
    <input ref={input} id="images" className="visually-hidden" type="file" accept="image/jpeg,image/png,image/webp" multiple onChange={e=>{add(e.target.files);e.target.value='';}}/>
    <div className={'dropzone '+(drag?'drag':'')} onDragOver={e=>{e.preventDefault();setDrag(true)}} onDragLeave={()=>setDrag(false)} onDrop={e=>{e.preventDefault();setDrag(false);add(e.dataTransfer.files)}}>
    {files.length ? <div className="thumbnails">{files.map((item,i)=><div className="thumbnail" key={item.url}><img src={item.url} alt={item.file.name}/><button type="button" aria-label={'移除 '+item.file.name} onClick={()=>setFiles(old=>old.filter((_,j)=>j!==i).map(f=>({file:f.file,url:URL.createObjectURL(f.file)})))}><X size={13}/></button></div>)}{files.length<5&&<button className="add-image" type="button" aria-label="添加参考图片" onClick={()=>input.current.click()}><Plus size={20}/></button>}</div> : <button className="upload-button" type="button" onClick={()=>input.current.click()}><ImagePlus size={25}/><strong>选择图片，或拖到这里</strong><span>JPG、PNG、WebP · 每张不超过 10 MB</span></button>}
    </div>
    {files.length>0&&<div className="image-options"><label>图片用途<select value={form.image_mode} onChange={e=>update('image_mode',e.target.value)}><option value="reference">参考画面与风格</option><option value="first_frame">作为起始画面（1 张）</option></select></label><label>素材来源<select value={form.provenance} onChange={e=>update('provenance',e.target.value)}><option value="owned">我的素材</option><option value="licensed">已获授权</option><option value="synthetic">AI 合成</option></select></label></div>}
    <div className="setting-row"><label htmlFor="duration">时长</label><select id="duration" value={form.duration} onChange={e=>update('duration',Number(e.target.value))}>{[3,4,5,6,8,10].map(n=><option value={n} key={n}>{n} 秒{n===4?' · 快速草稿':''}</option>)}</select></div>
    <div className="setting-row"><label htmlFor="aspect">画幅</label><select id="aspect" value={form.aspect} onChange={e=>update('aspect',e.target.value)}><option value="16:9">横屏（16:9）</option><option value="9:16">竖屏（9:16）</option></select></div>
    <details><summary>更多设置</summary><div className="setting-row"><label htmlFor="resolution">清晰度</label><select id="resolution" value={form.resolution} onChange={e=>update('resolution',e.target.value)}><option value="360p">360p · 预览草稿</option><option value="720p">720p</option><option value="1080p">1080p</option></select></div><p className="hint">高分辨率和更长视频通常消耗更多额度。不会自动重拍，也不会额外调用文案或审片模型。</p></details>
    <button className="primary generate" disabled={submitting||!form.brief.trim()|| (form.image_mode==='first_frame'&&files.length>1)}>{submitting?'正在提交…':'生成视频'}<ArrowUpRight size={19}/></button><p className="hint centered">每次只生成一个版本 · 将调用视频 API</p>
  </form></aside>
}
