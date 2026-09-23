export function financialPageScore(text:string):number{
 const score=(text.includes('合并利润表')?8:0)+(text.includes('营业收入')?5:0)+(text.includes('营业成本')?5:0)+(text.includes('净利润')?4:0)+(text.includes('本期发生额')?1:0)+(text.includes('上期发生额')?1:0);
 return score>=8&&/\d{1,3}[,，]\d{3}|\d{7,}/u.test(text)?score:0;
}
export function recommendedPages(scores:{page:number;score:number}[]):number[]{
 return scores.filter(item=>item.score>0).sort((a,b)=>b.score-a.score||a.page-b.page).slice(0,8).map(item=>item.page).sort((a,b)=>a-b);
}
export function pageSelectionError(raw:string,total:number|null):string{
 const input=raw.trim();
 if(!input)return total!==null&&total>32?'这份 PDF 超过 32 页，请选含财务表的页面。':'';
 const items=input.split(',').map(item=>item.trim());
 if(items.length>32)return '一次最多分析 32 页。';
 const pages=items.map(Number);
 if(pages.some((page,index)=>!/^\d+$/u.test(items[index])||!Number.isInteger(page)||page<1||page>10000))return '请用英文逗号分隔页码，例如 86,87。';
 if(new Set(pages).size!==pages.length)return '页码重复了，请只保留一次。';
 if(total!==null&&pages.some(page=>page>total))return `页码不能超过这份 PDF 的 ${total} 页。`;
 return '';
}
