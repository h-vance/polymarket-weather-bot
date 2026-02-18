import { useState, useEffect, Suspense, lazy } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { motion } from 'framer-motion'
import { fetchDashboard, startBot, stopBot } from './api'
import { StatsCards } from './components/StatsCards'
import { TradesTable } from './components/TradesTable'
import { EquityChart } from './components/EquityChart'
import { Terminal } from './components/Terminal'
import { WeatherPanel } from './components/WeatherPanel'

const GlobeView = lazy(() => import('./components/GlobeView').then(m => ({ default: m.GlobeView })))

function LiveClock() {
  const [time, setTime] = useState(new Date())
  useEffect(() => {
    const interval = setInterval(() => setTime(new Date()), 1000)
    return () => clearInterval(interval)
  }, [])
  return (
    <span className="text-xs tabular-nums text-neutral-400 font-mono">
      {time.toLocaleTimeString('en-US', { hour12: false })} UTC
    </span>
  )
}

function RefreshBar({ interval }: { interval: number }) {
  const [progress, setProgress] = useState(100)

  useEffect(() => {
    setProgress(100)
    const step = 100 / (interval / 1000)
    const timer = setInterval(() => {
      setProgress(p => Math.max(0, p - step))
    }, 1000)
    return () => clearInterval(timer)
  }, [interval])

  return (
    <div className="w-16 h-1 bg-neutral-900 overflow-hidden">
      <div className="h-full bg-cyan-500 transition-all duration-1000 ease-linear" style={{ width: `${progress}%` }} />
    </div>
  )
}

function App() {
  const queryClient = useQueryClient()

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ['dashboard'],
    queryFn: fetchDashboard,
    refetchInterval: 5000,
  })

  const startMutation = useMutation({
    mutationFn: startBot,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['dashboard'] }),
  })

  const stopMutation = useMutation({
    mutationFn: stopBot,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['dashboard'] }),
  })

  const recentTrades = data?.recent_trades ?? []
  const weatherSignals = data?.weather_signals ?? []
  const weatherForecasts = data?.weather_forecasts ?? []

  const stats = data?.stats ?? {
    is_running: false,
    last_run: null,
    total_trades: 0,
    total_pnl: 0,
    bankroll: 1000,
    winning_trades: 0,
    win_rate: 0
  }
  const equityCurve = data?.equity_curve ?? []

  const actionableCount = weatherSignals.filter(s => s.actionable).length

  if (isLoading) {
    return (
      <div className="h-screen bg-black flex items-center justify-center">
        <div className="text-center">
          <div className="relative w-10 h-10 mx-auto mb-4">
            <div className="absolute inset-0 border-2 border-neutral-800 rounded-full" />
            <div className="absolute inset-0 border-2 border-transparent border-t-cyan-500 rounded-full animate-spin" />
          </div>
          <div className="text-[10px] text-neutral-500 uppercase tracking-widest font-mono">Initializing CLOB...</div>
        </div>
      </div>
    )
  }

  if (error || !data) {
    return (
      <div className="h-screen bg-black flex items-center justify-center">
        <div className="text-center">
          <div className="text-red-500 text-xs uppercase mb-2 tracking-wider">CLOB Connection Error</div>
          <button
            onClick={() => refetch()}
            className="px-3 py-1.5 bg-neutral-900 border border-neutral-700 text-neutral-300 text-xs uppercase tracking-wider"
          >
            Reconnect
          </button>
        </div>
      </div>
    )
  }

  return (
    <div className="h-screen bg-black text-neutral-200 flex flex-col overflow-hidden">
      {/* ===== HEADER ===== */}
      <motion.header
        initial={{ opacity: 0, y: -10 }}
        animate={{ opacity: 1, y: 0 }}
        className="shrink-0 border-b border-neutral-800 px-3 py-1.5 flex items-center gap-4 relative"
      >
        <div className="flex items-center gap-2 shrink-0">
          <h1 className="text-xs font-bold text-neutral-100 uppercase tracking-widest whitespace-nowrap font-mono">
            POLYMARKET WEATHER CLOB
          </h1>
          <span className={`px-1.5 py-0.5 text-[9px] font-bold uppercase ${
            stats.is_running
              ? 'bg-cyan-500/10 text-cyan-500 border border-cyan-500/20'
              : 'bg-neutral-800 text-neutral-500 border border-neutral-700'
          }`}>
            {stats.is_running ? 'Live' : 'Idle'}
          </span>
        </div>

        <div className="flex-1" />

        <StatsCards stats={stats} />

        <div className="flex items-center gap-4 shrink-0 border-l border-neutral-800 pl-4">
          <LiveClock />
        </div>
      </motion.header>

      {/* ===== MAIN GRID ===== */}
      <div className="flex-1 min-h-0 grid grid-cols-[350px_1fr_400px] grid-rows-[1fr] gap-0">

        {/* ===== LEFT COLUMN ===== */}
        <div className="flex flex-col border-r border-neutral-800 min-h-0 overflow-hidden">
          
          {/* Equity chart */}
          <div className="border-b border-neutral-800" style={{ height: '30%' }}>
            <div className="px-2 py-1 border-b border-neutral-800 flex items-center justify-between shrink-0">
              <span className="text-[10px] text-neutral-500 uppercase tracking-wider font-mono">Portfolio Equity</span>
              <span className={`text-[10px] font-mono tabular-nums ${stats.total_pnl >= 0 ? 'text-green-500' : 'text-red-500'}`}>
                {stats.total_pnl >= 0 ? '+' : ''}${stats.total_pnl.toFixed(2)}
              </span>
            </div>
            <div className="h-[calc(100%-24px)] p-1">
              <EquityChart data={equityCurve} initialBankroll={stats.bankroll - stats.total_pnl} />
            </div>
          </div>

          {/* Terminal */}
          <div className="flex-1 min-h-0">
            <Terminal
              isRunning={stats.is_running}
              lastRun={stats.last_run}
              stats={{ total_trades: stats.total_trades, total_pnl: stats.total_pnl }}
              onStart={() => startMutation.mutate()}
              onStop={() => stopMutation.mutate()}
              onScan={() => {}} // Remove scan button, runs via scheduler
            />
          </div>
        </div>

        {/* ===== CENTER COLUMN ===== */}
        <div className="flex flex-col min-h-0 border-r border-neutral-800">
          {/* Globe - top 70% */}
          <div className="relative" style={{ height: '70%' }}>
            <div className="absolute inset-0">
              <Suspense fallback={
                <div className="w-full h-full flex items-center justify-center bg-black">
                  <span className="text-[10px] text-neutral-600 uppercase tracking-wider font-mono">Connecting to Satellite...</span>
                </div>
              }>
                <GlobeView forecasts={weatherForecasts} signals={weatherSignals} />
              </Suspense>
            </div>
            <div className="absolute top-2 left-2 z-10">
              <div className="px-2 py-1 bg-black/90 border border-neutral-800 text-[10px] font-mono">
                <span className="text-neutral-500 uppercase tracking-wider mr-2">Discovered Markets:</span>
                <span className="text-cyan-500 tabular-nums">{weatherSignals.length} active</span>
              </div>
            </div>
          </div>

          {/* Weather Panel - bottom 30% */}
          <div className="flex-1 min-h-0 border-t border-neutral-800 flex flex-col">
            <div className="px-2 py-1 border-b border-neutral-800 flex items-center justify-between shrink-0 bg-neutral-900/30">
              <span className="text-[10px] text-neutral-500 uppercase tracking-wider font-mono">NOAA Ensemble Forecasts</span>
            </div>
            <div className="flex-1 min-h-0 overflow-y-auto">
              <WeatherPanel forecasts={weatherForecasts} signals={weatherSignals} />
            </div>
          </div>
        </div>

        {/* ===== RIGHT COLUMN ===== */}
        <div className="flex flex-col min-h-0 overflow-hidden">
          
          <div className="flex flex-col min-h-0 border-b border-neutral-800" style={{ height: '50%' }}>
             <div className="px-2 py-1 border-b border-neutral-800 flex items-center justify-between shrink-0 bg-neutral-900/30">
              <span className="text-[10px] text-neutral-500 uppercase tracking-wider font-mono">Live CLOB Signals</span>
              <span className="text-[10px] text-cyan-400 tabular-nums font-mono">{actionableCount} Actionable</span>
            </div>
            <div className="flex-1 overflow-y-auto min-h-0 p-2 space-y-2">
               {weatherSignals.length > 0 ? (
                  weatherSignals.map(s => (
                    <div key={s.market_id} className={`p-2 border ${s.actionable ? 'border-cyan-500/50 bg-cyan-500/5' : 'border-neutral-800 bg-neutral-900/50'}`}>
                      <div className="flex justify-between items-center mb-1">
                        <span className="text-[10px] font-bold text-neutral-200 uppercase">{s.city_name}</span>
                        <span className={`text-[10px] font-mono font-bold ${s.direction === 'yes' ? 'text-green-500' : 'text-red-500'}`}>
                          {s.direction.toUpperCase()}
                        </span>
                      </div>
                      <div className="text-[9px] text-neutral-400 mb-2 font-mono">
                         {s.metric.toUpperCase()} {s.direction.toUpperCase()} {s.threshold_f}F on {s.target_date.split('T')[0]}
                      </div>
                      <div className="flex justify-between items-center text-[9px] font-mono">
                         <span className="text-neutral-500">NOAA: {(s.model_probability * 100).toFixed(1)}%</span>
                         <span className="text-neutral-500">CLOB: {(s.market_probability * 100).toFixed(1)}%</span>
                         <span className={`font-bold ${s.edge > 0 ? 'text-green-400' : 'text-neutral-500'}`}>
                           Edge: {(s.edge * 100).toFixed(1)}%
                         </span>
                      </div>
                    </div>
                  ))
               ) : (
                  <div className="text-[10px] text-neutral-600 font-mono">No signals discovered.</div>
               )}
            </div>
          </div>

          {/* Trades */}
          <div className="flex flex-col min-h-0" style={{ height: '50%' }}>
            <div className="px-2 py-1 border-b border-neutral-800 flex items-center justify-between shrink-0 bg-neutral-900/30">
              <span className="text-[10px] text-neutral-500 uppercase tracking-wider font-mono">Execution Log</span>
              <span className="text-[10px] text-neutral-600 tabular-nums font-mono">{recentTrades.length}</span>
            </div>
            <div className="flex-1 overflow-y-auto min-h-0">
              <TradesTable trades={recentTrades} />
            </div>
          </div>
        </div>
      </div>

      {/* ===== FOOTER ===== */}
      <footer className="shrink-0 border-t border-neutral-800 px-3 py-1 flex items-center justify-between">
        <span className="text-[10px] text-neutral-700 font-mono uppercase">
          Open-Meteo GFS Ensemble | Polymarket CLOB
        </span>
        <div className="flex items-center gap-3">
          <RefreshBar interval={5000} />
          <span className="text-[10px] text-neutral-700 font-mono uppercase">Weather Strategy</span>
          <div className="flex items-center gap-1">
            <div className="w-1.5 h-1.5 rounded-full bg-cyan-500 shadow-[0_0_5px_rgba(6,182,212,0.5)]" />
            <span className="text-[10px] text-cyan-500/70 font-mono uppercase">L2 Synced</span>
          </div>
        </div>
      </footer>
    </div>
  )
}

export default App