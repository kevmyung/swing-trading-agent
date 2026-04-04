import { BrowserRouter, Routes, Route } from 'react-router-dom';
import { Layout } from '@/components/Layout';
import { SessionList } from '@/pages/SessionList';
import { SessionDetail } from '@/pages/SessionDetail';
import { NewBacktest } from '@/pages/NewBacktest';
import { DataRefresh } from '@/pages/DataRefresh';
import { PaperTrading, LiveTradingPage } from '@/pages/LiveTrading';
import { Settings } from '@/pages/Settings';
import { Analysis } from '@/pages/Analysis';

function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route element={<Layout />}>
          <Route path="/" element={<SessionList />} />
          <Route path="/new" element={<NewBacktest />} />
          <Route path="/analysis" element={<Analysis />} />
          <Route path="/data" element={<DataRefresh />} />
          <Route path="/sessions/:sessionId" element={<SessionDetail />} />
          <Route path="/live/paper" element={<PaperTrading />} />
          <Route path="/live/real" element={<LiveTradingPage />} />
          <Route path="/settings" element={<Settings />} />
        </Route>
      </Routes>
    </BrowserRouter>
  );
}

export default App;
