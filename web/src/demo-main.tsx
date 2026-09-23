import {useMemo} from 'react';
import {createRoot} from 'react-dom/client';
import {CrackRAGWorkspace} from './main';
import {ReplayScope} from './demo/replay';
import sessionJSON from './demo/session.json';
import type {ReplaySession} from './demo/types';
import './style.css';

const session = sessionJSON as unknown as ReplaySession;

function DemoNotice() {
  return (
    <section className="card replay-notice" aria-label="离线回放说明">
      <div className="replay-badge">离线回放 · 冻结自真实运行</div>
      <p>
        点击查看一次已记录的年报问答：从第一次回答、原页核对，到再次提问直接复用。
        本页不连接后端，不调用模型，也不产生新费用。
      </p>
      <p className="replay-proof">{session.notice.banner}</p>
      <p className="hint">对应正式版本 {session.product.release}；真实段与最后的模拟缺证据示例已分别标注。</p>
      <div className="replay-actions">
        <button type="button" className="secondary" onClick={() => window.location.reload()}>重新播放</button>
        <a className="text-button" href={session.notice.evidence_url}>查看原始真实演示视频</a>
        <a className="text-button" href="https://github.com/XJfyrh/CrackRAG">项目源码与验收记录</a>
      </div>
    </section>
  );
}

function Demo() {
  // One replay scope for the lifetime of the page: the sequence is immutable.
  const scope = useMemo(() => new ReplayScope(session), []);
  return (
    <CrackRAGWorkspace
      scope={scope}
      readOnly
      demoSteps={session.queries.map(({role,question,document_ids,build_facts,execution_policy})=>({role:role||'',question,document_ids,build_facts,execution_policy}))}
      notice={<DemoNotice/>}
      onTokenChange={() => window.location.reload()}
    />
  );
}

function MissingSession() {
  return (
    <main className="login">
      <section className="card">
        <p className="eyebrow">DEMO BUILD</p>
        <h1>缺少回放会话</h1>
        <p>此构建需要 <code>web/src/demo/session.json</code>。请按 <code>docs/demo-site.md</code> 重新录制。</p>
      </section>
    </main>
  );
}

createRoot(document.getElementById('root')!).render(
  session?.schema === 'crackrag-demo-replay-v1' ? <Demo/> : <MissingSession/>,
);
