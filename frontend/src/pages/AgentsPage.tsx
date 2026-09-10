import { useState } from "react";
import { Activity, Bot, CheckCheck, CircleAlert, Clock3, Search, ShieldCheck, SlidersHorizontal, Target, ChartNoAxesCombined, ClipboardCheck, Globe, Layers, Wallet } from "lucide-react";
import { useDesk } from "@/context/DeskContext";
import { useI18n } from "@/i18n/I18nProvider";
import { agentDisplayStatus } from "@/lib/api";
import styles from "./AgentsPage.module.css";

const roles = {
  scanner: { icon: Search, ru: ["Сканер", "Отбирает инструменты и запускает проверку торговых условий."], en: ["Scanner", "Selects instruments and starts checking trade conditions."] },
  context: { icon: Globe, ru: ["Контекст рынка", "Оценивает рыночную обстановку для торговой идеи."], en: ["Market context", "Assesses market conditions for a trade idea."] },
  universe: { icon: Layers, ru: ["Отбор инструментов", "Проверяет пригодность инструмента для анализа."], en: ["Universe", "Checks whether an instrument is suitable for analysis."] },
  structure: { icon: ChartNoAxesCombined, ru: ["Структура цены", "Анализирует движение цены и ключевые уровни."], en: ["Price structure", "Analyses price movement and key levels."] },
  setup: { icon: Target, ru: ["Торговая формация", "Проверяет условия формирования торговой идеи."], en: ["Setup", "Checks the conditions that form a trade setup."] },
  entry: { icon: SlidersHorizontal, ru: ["Условия входа", "Оценивает готовность торговой идеи ко входу."], en: ["Entry", "Evaluates whether a trade setup is ready for entry."] },
  risk_plan: { icon: ShieldCheck, ru: ["План риска", "Формирует параметры риска для торговой идеи."], en: ["Risk plan", "Prepares risk parameters for a trade idea."] },
  checklist: { icon: ClipboardCheck, ru: ["Проверка условий", "Сверяет торговую идею с обязательными условиями."], en: ["Checklist", "Checks a trade idea against required conditions."] },
  risk: { icon: ShieldCheck, ru: ["Контроль риска", "Проверяет лимиты и допустимость сделки."], en: ["Risk engine", "Checks limits and whether a trade is permitted."] },
  position: { icon: Wallet, ru: ["Контроль позиций", "Проверяет открытые позиции и условия выхода."], en: ["Position monitor", "Reviews open positions and exit conditions."] },
  review: { icon: CheckCheck, ru: ["Анализ результатов", "Оценивает результаты сделок по торговому журналу."], en: ["Review", "Evaluates trade outcomes from the trading journal."] },
};
type Status = "all" | "working" | "done" | "idle" | "error" | "unknown";

export function AgentsPage() {
  const { desk } = useDesk();
  const { locale } = useI18n();
  const ru = locale === "ru";
  const [filter, setFilter] = useState<Status>("all");
  const labels: Record<Status, string> = ru
    ? { all: "Все агенты", working: "Работают", done: "Завершили", idle: "Ожидают", error: "Ошибка", unknown: "Нет статуса" }
    : { all: "All agents", working: "Working", done: "Completed", idle: "Waiting", error: "Error", unknown: "Unknown" };
  const agents = desk?.activity.agents ?? [];
  const statusOf = (agent: typeof agents[number]): Status => {
    const status = agentDisplayStatus(agent);
    return ["working", "done", "idle", "error"].includes(status) ? status as Status : "unknown";
  };
  const visible = agents.filter(agent => filter === "all" || statusOf(agent) === filter);
  const stats = [
    { status: "all" as const, icon: Bot },
    { status: "working" as const, icon: Activity },
    { status: "idle" as const, icon: Clock3 },
    { status: "error" as const, icon: CircleAlert },
  ];

  return <div className={styles.page}>
    <header className={styles.heading}>
      <div><h1>{ru ? "Команда агентов" : "Agent team"}</h1>
        <p>{ru ? "Кто за что отвечает и на каком этапе находится работа." : "Responsibilities and the current state of each agent."}</p></div>
      <span className={styles.venue}>Alpaca Paper</span>
    </header>
    <div className={styles.summary}>
      {stats.map(({ status, icon: Icon }) => <div className={styles.stat} key={status}>
        <span><Icon size={16} aria-hidden="true" />{labels[status]}</span>
        <strong>{desk ? (status === "all" ? agents.length : agents.filter(a => statusOf(a) === status).length) : "—"}</strong>
      </div>)}
    </div>
    <section aria-label={ru ? "Состояния агентов" : "Agent states"}>
      <div className={styles.toolbar}>
        <div className={styles.filters} aria-label={ru ? "Фильтр состояния" : "Status filter"}>
          {(["all", "working", "done", "idle", "error"] as const).map(status =>
            <button key={status} type="button" aria-pressed={filter === status} onClick={() => setFilter(status)}>{labels[status]}</button>)}
        </div>
        <span className={styles.note}>{ru ? "Последнее полученное состояние" : "Last received state"}</span>
      </div>
      {!desk || visible.length === 0 ? <div className={styles.empty} role="status">
        <Bot size={28} aria-hidden="true" />
        <h2>{!desk ? (ru ? "Данные агентов пока не получены" : "Agent data has not arrived yet") : agents.length === 0 ? (ru ? "Агенты пока не передали состояния" : "No agent states reported yet") : (ru ? "В этом состоянии нет агентов" : "No agents in this state")}</h2>
        <p>{!desk ? (ru ? "Состояния появятся после получения данных от сервера." : "States will appear once server data is received.") : (ru ? "Список обновляется автоматически." : "The list updates automatically.")}</p>
      </div> : <div className={styles.grid}>
        {visible.map(agent => {
          const role = roles[agent.id as keyof typeof roles];
          const Icon = role?.icon ?? Bot;
          const copy = role?.[ru ? "ru" : "en"];
          const status = statusOf(agent);
          const date = agent.updated_at ? new Date(agent.updated_at) : null;
          const validDate = date && !Number.isNaN(date.getTime());
          return <article key={agent.id} className={styles.card} data-status={status}>
            <div className={styles.cardTop}><span className={styles.icon}><Icon size={21} aria-hidden="true" /></span>
              <span className={styles.badge}><i />{labels[status]}</span></div>
            <h2>{copy?.[0] ?? agent.name}</h2>
            <p className={styles.description}>{copy?.[1] ?? (ru ? "Агент подключён к торговой системе." : "Agent connected to the trading system.")}</p>
            <dl className={styles.facts}>
              <div><dt>{ru ? "Последний тикер" : "Last symbol"}</dt><dd>{agent.last_symbol || "—"}</dd></div>
              <div><dt>{ru ? "Обновление · Нью-Йорк" : "Updated · New York"}</dt><dd>{validDate ? <time dateTime={agent.updated_at!} title={date.toISOString()}>{date.toLocaleString(ru ? "ru-RU" : "en-US", { timeZone: "America/New_York", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false })}</time> : "—"}</dd></div>
            </dl>
            {status === "idle" && !agent.updated_at ? <p className={styles.hint}>{ru ? "После запуска ещё не передавал обновлений" : "No updates reported since startup"}</p> : null}
            {status === "error" ? <p className={styles.error}>{ru ? "Последняя задача завершилась с ошибкой. Подробности — на странице логов." : "The last task failed. Details are available on the logs page."}</p> : null}
          </article>;
        })}
      </div>}
    </section>
  </div>;
}
