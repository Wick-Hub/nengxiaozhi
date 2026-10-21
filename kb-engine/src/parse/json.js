export function jsonToBlocks(obj) {
  if (Array.isArray(obj)) {
    const blocks = [];
    for (const item of obj) {
      if (item && (item.q || item.question)) {
        const q = item.q || item.question;
        const a = item.a || item.answer || '';
        const variants = Array.isArray(item.variants) ? item.variants.join('；') : (item.variants || '');
        blocks.push({
          type: 'qa',
          text: `问：${q}\n答：${a}${variants ? `\n同义问法：${variants}` : ''}`,
        });
      } else if (item && typeof item === 'object') {
        blocks.push({ type: 'para', text: JSON.stringify(item) });
      }
    }
    return blocks;
  }
  return [{ type: 'para', text: JSON.stringify(obj, null, 1) }];
}
