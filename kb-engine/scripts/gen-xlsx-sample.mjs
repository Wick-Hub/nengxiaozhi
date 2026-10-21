import * as XLSX from 'xlsx';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

const rows = [
  ['问题', '答案', '同义问法'],
  [
    '发票丢失了还能报销吗？',
    '可以。票据遗失的，需提供票据复印件或电子发票打印件，并填写《票据遗失情况说明》，经单位负责人签字后按流程报销；无法提供任何凭证的部分不予报销。',
    '发票丢了怎么办;没有发票能报吗;票据遗失如何处理',
  ],
  [
    '网上报销系统忘记密码怎么办？',
    '持本人身份证到财务处报销科重置，或通过网上办事大厅"密码找回"功能自助重置。',
    '报销系统密码忘了;报销平台登录不了',
  ],
  [
    '报销款多久到账？',
    '财务审核通过后一般 5-10 个工作日到账；年底集中报销期间可能延长至 15 个工作日。',
    '钱什么时候到账;报销进度怎么查;报销款为什么还没到',
  ],
  [
    '学生可以报销科研经费吗？',
    '可以。学生参与导师课题出差，费用可在导师科研经费中报销，出差审批单须由导师签字确认。',
    '学生出差怎么报销;研究生能报科研经费吗',
  ],
];

const ws = XLSX.utils.aoa_to_sheet(rows);
const wb = XLSX.utils.book_new();
XLSX.utils.book_append_sheet(wb, ws, '财务报销FAQ');

const out = path.resolve(__dirname, '../../knowledge-base/02-财务处/常见问题/财务报销常见问题.xlsx');
XLSX.writeFile(wb, out);
console.log(`已生成示例 xlsx：${out}`);
