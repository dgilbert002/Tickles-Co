# What We're Building — Plain English

> This is the non-technical explanation of the Intelligence Pipeline Architecture.
> If you want the technical specs, see `INTELLIGENCE_PIPELINE_DESIGN.md`.

---

## The Situation Right Now

Your platform is like a very diligent intern who collects every message from Discord, Telegram, and RSS feeds, puts them in a filing cabinet, and then does nothing with them. The messages are stored. We know if they're positive or negative. We know what symbols they mention. But that's where it stops. It's like having a library full of books and no one reading them.

What we're building is the brain that reads those books and decides what to do about them.

---

## Problem One: We Don't Know Who's Talking

When someone called "CryptoWhale99" posts a bullish message on Discord, we store the message. When the same person posts on Telegram an hour later, we store that too. But we have no idea it's the same person. We can't track whether CryptoWhale99 is actually good at predicting markets because we have no unified record of who they are.

**What we're building:** A trader profile system. Think of it as a business card database. Every time we see a new username on any platform, we create a card for them. The card says: this is who they are, where we first saw them, whether they're verified by the platform, and whether they're a bot. Now when CryptoWhale99 posts on Discord and Telegram, both messages link back to the same card. This means we can finally track their performance across every platform they use.

**Why this matters:** Without knowing who is who, you can't weight signals. A message from someone who has been right seventy percent of the time over three months should count for more than a message from someone who joined yesterday and has been wrong every time. Right now, both messages have equal weight in your system. That's like treating a weather forecast from a meteorologist the same as a guess from your uncle.

---

## Problem Two: We Don't Know What the Messages Mean

Currently, enrichment tells us a message is "positive" and mentions "BTC/USDT." But "positive" about what? Does it mean buy now? Does it mean hold? Does it mean "BTC looks interesting but I'm not trading it"? The system has no opinion. It's just a filing cabinet.

**What we're building:** An interpretation layer. This is where the system actually reads the message and decides: "This person is saying we should open a long position on Bitcoin with medium confidence." It does this by running two separate analyses in parallel and then comparing them.

The first analysis is the LLM track. Essentially, we show the message to a large language model like GPT and ask it to extract the trading direction, confidence level, and rationale. The LLM reads the text, looks at any attached charts, and gives us a structured opinion.

The second analysis is the quant track. This is a purely mathematical model that looks at the actual market data at the time the message was posted. It checks things like: was RSI overbought when this person said buy? Was there a bearish divergence on the four-hour chart? The quant model doesn't read the text; it reads the numbers.

Then we have a consensus engine that compares the two opinions. If the LLM says "long" and the quant model says "long," we have high confidence. If the LLM says "long" but the quant model says "short because RSI is eighty-five," we flag it as a conflict. A conflict isn't a failure. It's valuable information. It means there's high uncertainty, and the system should either reduce position size or skip the trade entirely.

**Why two tracks instead of one?** Because LLMs hallucinate. They see patterns in charts that aren't there. They get excited by narrative and miss hard data. Quant models are blind to narrative. They don't know that a Fed announcement just happened, or that a major exchange got hacked. By running both and looking for agreement, we catch things either one would miss alone. When they disagree, that disagreement is itself a signal: the market is confused, and we should be cautious.

We also capture the market context at the exact moment of interpretation. Was this message posted during the London open, when volume is high? Or at three AM on a Sunday, when moves are often fake-outs? Was Bitcoin at sixty thousand or ninety thousand? Context changes everything. A bullish call at all-time highs with funding rates at point one percent is very different from a bullish call after a thirty percent drawdown with funding negative.

---

## Problem Three: Charts and Screenshots Are Dead Weight

People post screenshots of TradingView charts all the time. They post annotated charts with trendlines, support levels, and indicator readings. Right now, your system stores these images but never looks at them. It's like receiving a detailed map and putting it in a drawer.

**What we're building:** The ChartHacker agent. Like the trading agents, this is created through Paperclip and OpenClaw — it is not a standalone script running on its own. It has its own workspace folder with a SOUL.md file that tells it how to read chart images, what to look for, and what format to output. It runs on a cron schedule managed by OpenClaw, just like the trading agents run every five minutes.

The ChartHacker looks at chart images and extracts structured data from them. It can read a screenshot and tell you: "This is a Bitcoin four-hour chart. The person drew a trendline from sixty-two thousand to sixty-eight thousand. RSI is showing seventy-two. There's a support level marked at fifty-nine thousand five hundred."

It does this using vision-capable AI models through OpenRouter, managed by OpenClaw. We prefer to use a TradingView MCP server if one is available. This is essentially an API that can read TradingView chart data directly, which is faster and cheaper than analyzing images. But if that server isn't available, the vision model acts as a fallback. Both approaches are logged in the agent's TRADE_LOG.md so we can compare their accuracy over time.

**Why this matters:** A huge percentage of trading signals are visual. Text-only analysis misses half the story. When someone posts a chart with a head-and-shoulders pattern and you only read their caption, you might miss that the pattern is actually invalid because the neckline is broken. The ChartHacker ensures we see what they see.

---

## Problem Four: We Never Learn Who's Actually Good

Your system ingests thousands of messages but never looks back to see who was right. It's like hiring a hundred consultants, listening to all of them, and never checking whose advice made you money.

**What we're building:** A performance scorer that runs every night at midnight. It looks back at all the directional calls made by tracked traders over the past day, week, or month, and compares them to what actually happened in the market. Did they say "long Bitcoin" and did Bitcoin go up over the next twenty-four hours? Did they say "short Ethereum" and did Ethereum drop?

But we go further than simple direction. We also score timing. Did they call the entry near the optimal point, or did they call it after the move was already half over? We score risk. Did they recommend ten times leverage on every trade, or were they conservative? We calculate a composite score that blends accuracy, timing, and risk-adjusted returns.

This creates a reputation system. Over time, you know that Trader A has a composite score of zero point eight two and specializes in Ethereum breakouts during the London session. Trader B has a score of zero point three four and mostly posts during emotional market moments. When both post at the same time, the system can weight Trader A's opinion higher.

**Why this matters:** Markets are full of noise. Most traders on social media are wrong more often than they're right. Without tracking performance, you're treating a coin flip as a signal. With performance tracking, you can build a portfolio of signal sources the same way you build a portfolio of assets: diversified, weighted by proven edge.

---

## Problem Five: Every Company Lives in a Silo

Your platform serves multiple companies. Think of them as separate trading desks or clients. Right now, if Company A discovers that a particular Telegram channel is consistently accurate, Company B has no way to know that. Each company is reinventing the wheel.

**What we're building:** A shared trader catalog but per-company scoring and interpretation. The trader profiles are global. Everyone sees the same business cards. But the performance scores and interpretations are per-company because different companies have different strategies, risk tolerances, and time horizons. Company A might care about twenty-four-hour accuracy. Company B might care about four-hour scalps. Both can use the same raw data but score it differently.

There's also a shared insights namespace in the memory system where anonymized patterns can be published. If three companies independently discover that a particular Discord server is highly accurate during NFP releases, that pattern can be shared without revealing any company's specific positions or weights.

**Why this matters:** Intelligence is expensive to generate. If five companies all pay to analyze the same Telegram channel, that's wasted effort. By sharing the "who" and keeping the "what we think of them" private, everyone benefits from collective discovery while keeping their edge proprietary.

---

## How It All Flows Together

Here's what happens when a new message arrives. The Discord collector grabs it, groups it with any nearby messages from the same person, and stores it. If this is the first time we've seen this username, a trader profile is created automatically. The enrichment pipeline adds sentiment and detects any symbols mentioned. If there's a chart image, the ChartHacker agent — running as an OpenClaw-managed agent just like the trading agents — analyzes it and extracts structured data.

Every five minutes, the interpretation service wakes up, grabs all newly enriched messages, and runs the dual-track analysis. The LLM reads the text and chart. The quant model reads the market data. The consensus engine blends them. The result is stored with full context: what the market looked like, what session it was, what models were used, and what parameters. Everything is hashed so we can reproduce the exact same interpretation later for validation.

Every night, the performance scorer looks back at the past day's calls, compares them to actual market movement, and updates each trader's score. Over weeks and months, a clear picture emerges of who is worth listening to and who is noise.

When a company wants to act on a signal, they don't just see "someone said buy Bitcoin." They see: "This is a high-confidence long signal from a trader with a zero point seven eight composite score, confirmed by both narrative and quantitative analysis, posted during high-volume London session, with market context showing funding rates neutral and RSI not overbought." That's a very different decision than acting on a raw message.

---

## What Could Go Wrong, and How We Guard Against It

If the LLM API goes down or hits rate limits, the system falls back to the quant track alone. It keeps working, just with one brain instead of two. If the vision model for chart analysis becomes too expensive, we can disable it per-company or fall back to the TradingView MCP server. If a trader creates fake accounts to game the performance system, we require a minimum number of calls before scoring kicks in, and verified accounts get priority.

The twenty-four-hour scoring window might feel slow if a trader is on a hot streak, but we can add shorter windows without changing anything in the database. The system is designed to be extended, not rewritten.

---

## In Plain English: What We're Really Building

We're turning your platform from a message storage system into a signal intelligence system. Right now you have a library. We're adding the librarians who read every book, the analysts who check which authors were right, the translators who turn vague language into clear directions, and the risk managers who say "this looks good but the quant model disagrees, so let's be careful."

Every piece exists for a reason. The trader profiles exist because identity matters. The dual interpretation exists because no single method is trustworthy alone. The chart hacker exists because traders communicate visually, not just in text. The performance scorer exists because track record is the only real edge in markets. And the shared catalog exists because intelligence should compound, not fragment.

When this is done, a message won't just be stored. It will be understood, contextualized, scored, and either acted upon or deliberately ignored, with a full audit trail of why.
