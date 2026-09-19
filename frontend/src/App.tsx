import { useEffect } from 'react'
import { BrowserRouter, NavLink, Route, Routes } from 'react-router-dom'
import { ModeBanner } from './components/ModeBanner'
import { Backtest } from './pages/Backtest'
import { Chart } from './pages/Chart'
import { Committee } from './pages/Committee'
import { Market } from './pages/Market'
import { Micro } from './pages/Micro'
import { Options } from './pages/Options'
import { Overview } from './pages/Overview'
import { Risk } from './pages/Risk'
import { Signals } from './pages/Signals'
import { System } from './pages/System'
import { Trades } from './pages/Trades'
import { useEngine } from './store/engine'

const PAGES = [
  ['/', 'Overview'],
  ['/chart', 'Chart'],
  ['/signals', 'Signals'],
  ['/trades', 'Trades'],
  ['/risk', 'Risk'],
  ['/backtest', 'Backtest'],
  ['/micro', 'Microstructure'],
  ['/options', 'Options'],
  ['/market', 'Market'],
  ['/committee', 'Committee'],
  ['/system', 'System'],
] as const

export default function App() {
  const { health, error, refresh } = useEngine()
  useEffect(() => {
    void refresh()
    const id = setInterval(() => void refresh(), 5000)
    return () => clearInterval(id)
  }, [refresh])

  return (
    <BrowserRouter>
      <ModeBanner health={health} error={error} />
      <div className="flex min-h-screen">
        <nav className="w-44 border-r border-slate-800 p-3 space-y-1">
          <div className="px-2 pb-3 text-sm font-bold tracking-wide">kotsin-crypto</div>
          {PAGES.map(([to, label]) => (
            <NavLink key={to} to={to} end={to === '/'} className={({ isActive }) => `block rounded px-2 py-1 text-sm ${isActive ? 'bg-slate-800 text-white' : 'text-slate-400 hover:text-white'}`}>
              {label}
            </NavLink>
          ))}
        </nav>
        <main className="flex-1 min-w-0">
          <Routes>
            <Route path="/" element={<Overview />} />
            <Route path="/chart" element={<Chart />} />
            <Route path="/signals" element={<Signals />} />
            <Route path="/trades" element={<Trades />} />
            <Route path="/risk" element={<Risk />} />
            <Route path="/backtest" element={<Backtest />} />
            <Route path="/micro" element={<Micro />} />
            <Route path="/options" element={<Options />} />
            <Route path="/market" element={<Market />} />
            <Route path="/committee" element={<Committee />} />
            <Route path="/system" element={<System />} />
          </Routes>
        </main>
      </div>
    </BrowserRouter>
  )
}
