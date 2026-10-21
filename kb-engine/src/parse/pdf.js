import pdf from 'pdf-parse/lib/pdf-parse.js';

// pdf.js 仅能抽取文本层的 PDF；扫描件（图片型 PDF）需 OCR，生产环境由 MinerU/DeepDoc 处理
export async function pdfToBlocks(buffer) {
  const data = await pdf(buffer);
  const para = [];
  const blocks = [];
  const flush = () => {
    const t = para.join(' ').trim();
    if (t) blocks.push({ type: 'para', text: t });
    para.length = 0;
  };
  for (const line of String(data.text).split(/\r?\n/)) {
    const t = line.trim();
    if (!t) { flush(); continue; }
    para.push(t);
  }
  flush();
  return blocks;
}
