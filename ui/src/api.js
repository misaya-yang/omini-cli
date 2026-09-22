export async function api(path, options={}) {
  const response = await fetch('/api'+path, {...options, headers:{'X-Studio-Request':'1',...options.headers}});
  let data; try { data = await response.json(); } catch { throw new Error('工作台连接中断，请检查本地服务是否运行。'); }
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '输入格式有误，请检查后重试。');
  return data;
}
export const post = (path, body) => api(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
