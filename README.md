# Heros Agents

**https://heros-agent.space** — five agents that do real work and say plainly what they did not,
plus **Market watch**: what could move the US stock market over the next two weeks, and which way.
The whole site is in English and 中文.

## The agents

| Agent | What it does | Where |
|---|---|---|
| **Heros** | Evaluates an AI agent: point her at a repository, she assesses it across nine axes, and opens a pull request only once you say yes | https://eval.heros-agent.space |
| **阿桥 Opportunity Bridge** | Finds real jobs, training and subsidies, and reads the public conditions out to you one by one; never decides eligibility for you | https://jobs.heros-agent.space |
| **FORGE** | Engineering design out loud: describe a part, get a shape you can rotate, section and measure, with the reasoning behind every decision | https://forge.heros-agent.space |
| **Vera · 维拉** | Prepares legal cases and looks after your health; licensed attorneys and physicians sign off on everything that matters | https://act.heros-agent.space |
| **Aoi · 葵** | Game night: Texas Hold’em with friends and AI players, and board games designed from your description; play money only | https://play.heros-agent.space |

They share three habits: **ask before guessing**, **a person signs off** on anything that costs money or
can’t be undone, and **keep the record** of who asked, what ran and what changed.

## Market watch

**The question it answers:** *what is scheduled over the next two weeks that could move the Dow Jones,
the Nasdaq Composite and the S&P 500, and which way is each index more likely to go?*

### What you see

| Part | What it shows |
|---|---|
| **Index strip** | The last close and daily change for the Dow, Nasdaq and S&P 500, plus the VIX and the 10-year Treasury yield |
| **Timeline** | Every trading day in the next two weeks, in order. Each day opens with a **session call** for each index. Under it are the day’s events, each with its time (Eastern), category, impact level, consensus and prior figures, and an **up ▲ or down ▼ call for each index, with a probability and how big the reaction is likely to be** |
| **Calendar** | Monday-to-Friday weeks. Each day shows how many events it has, how many are high impact, and the three session calls. Pick a day to see its events. Past days show what the index actually did, with ✓ or ✗ against the call |
| **Track record** | How often the calls were right for each index, and a Brier score (0.25 is a coin flip; lower is better) |
| **Event details** | A one-line read of the event, the upside and downside cases, what to watch, how confident the call is, and where the data came from |
| **How this was made** | When it was last updated, how many sources answered, whether any analysis was sent back and redone, and whether the calls came from the AI analyst or the rules-based baseline |
| **Headlines** | The latest market-moving headlines |

You can filter to **Fed & macro**, **earnings** or **high impact only**. The page remembers your view and
language, and works on a phone.

### What it covers

| Category | Events |
|---|---|
| **Fed** | FOMC rate decisions (including the meetings with the dot plot), minutes, the Fed Chair’s speeches |
| **Inflation** | CPI, PCE, PPI |
| **Jobs** | The monthly jobs report (payrolls, unemployment, wages), JOLTS job openings, ADP, weekly jobless claims |
| **Growth & consumer** | GDP, ISM manufacturing and services, retail sales, consumer confidence and sentiment, durable goods, housing |
| **Earnings** | Companies worth $200 billion or more, marked when they are in the Dow or are Nasdaq heavyweights |

High-impact events (FOMC, CPI, PCE, the jobs report, GDP, the largest companies’ earnings) usually move
all three indices. Low-impact ones rarely move any. The timeline lists the most important first, up to
36 events.

Data comes from the Federal Reserve, Nasdaq’s economic and earnings calendars, Yahoo Finance for index
levels, and CNBC and MarketWatch for headlines.

### How a call is made

1. **A baseline** starts from what it can show: the market’s long-run tendency to rise, the past week’s
   momentum, what consensus expects compared with last time, and for earnings, expected growth. Once a
   figure is released, the **surprise versus consensus** takes over. A hot inflation print usually hurts
   stocks, the Nasdaq most; strong growth usually helps, the Dow most.
2. **The AI analyst** reads the events, the market’s levels and the headlines, sees the baseline, and
   makes its own call for each index: up or down, a probability, and the likely size of the move, with
   a short explanation in both languages. If it disagrees with the baseline, it says why.
3. **An independent critic** checks every analysis. It sends one back if it cites a number that isn’t
   in the data, sounds like investment advice, or is more certain than the evidence allows. A rejected
   analysis is redone once. If it still fails, the page shows the baseline call and says so.
4. **The session call** for each day combines that day’s events, weighted by how much each index is
   exposed to each one. A quiet day leans on the market’s longer-run tendency.
5. **Every call is scored.** A day’s call is locked before the 9:30 AM ET open and can’t be changed
   afterwards. After the close it is checked against what the index actually did, and the result goes
   into the track record.

### How sure it is

Before a release, the probabilities stay modest: at most 66% from the baseline and 85% from the AI
analyst. After the figure is out, the baseline can reach 80%. Most calls sit close to 50–60%, because
honestly that is how much anyone can know in advance. The track record is there so you can judge the
calls for yourself.

### When it updates

Three times each weekday, at **7:30 AM, 12:00 PM and 4:30 PM Eastern**, and again **15 minutes after each
major release**, so the timeline shows the actual figure and a fresh call.

### What it is not

Market watch is AI-generated market commentary, for information only. It is **not investment advice**:
no buy or sell recommendations, no price targets, no single-stock picks. Market holidays are not shown
as closed yet.

## Support

Every agent is built and run independently. Sponsor them on
[Patreon](https://patreon.com/damonleelcx) or Afdian (爱发电), or write to
[support@heros-agent.space](mailto:support@heros-agent.space).

---

Agents are AI. They do not replace licensed professionals or official decisions. Games use play money only.

*For developers: running and deploying the site is in [deploy/README.md](deploy/README.md); the
multi-agent system behind Market watch is in [market/README.md](market/README.md) and
[market/DESIGN.md](market/DESIGN.md).*
