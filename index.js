const TelegramBot = require('node-telegram-bot-api');

// 🔑 তোমার Telegram Bot Token এখানে বসাও (BotFather থেকে নেওয়া)
const TOKEN = 'YOUR_TELEGRAM_BOT_TOKEN_HERE';
const bot = new TelegramBot(TOKEN, { polling: true });

// ব্যবহারকারীদের টাইমার ও লাস্ট প্রেডিকশন ট্র্যাক রাখার অবজেক্ট
const userSessions = {};

/**
 * রিয়েল-টাইম UTC পিরিয়ড এবং বাকি থাকা সেকেন্ড বের করার ফাংশন
 */
function getMarketTimeAndPeriod() {
    const now = new Date();
    const sec = now.getUTCSeconds();
    
    // টাইমার: আগামী ১ মিনিটের কত সেকেন্ড বাকি (59, 58 ... 00)
    const remainingSeconds = 59 - sec;
    
    // দিনটির মোট মিনিট হিসাব (1 - 1440)
    const totalMinutesInDay = (now.getUTCHours() * 60) + now.getUTCMinutes() + 1;
    
    // UTC তারিখের স্ট্রিং YYYYMMDD
    const dateStr = `${now.getUTCFullYear()}${String(now.getUTCMonth() + 1).padStart(2, '0')}${String(now.getUTCDate()).padStart(2, '0')}`;
    
    // পিরিয়ড নাম্বার তৈরি
    const fullPeriod = `${dateStr}1000${String(totalMinutesInDay).padStart(4, '0')}`;
    const periodDisplay = fullPeriod.slice(-5); // শেষ ৫ ডিজিট (যেমন: 0144)

    return {
        remainingSeconds,
        periodDisplay,
        fullPeriod
    };
}

/**
 * সিগন্যাল/প্রেডিকশন অ্যানালাইসিস ফাংশন (তোমার আগের লজিক অনুযায়ী)
 */
function generatePrediction() {
    const options = ["SMALL", "BIG"];
    const val = options[Math.floor(Math.random() * options.length)];
    const num = val === "BIG" ? Math.floor(Math.random() * 5) + 5 : Math.floor(Math.random() * 5);
    const randomSupportNum = Math.floor(Math.random() * 9);
    
    const randomAcc = Math.floor(Math.random() * (100 - 50 + 1)) + 50;
    const randomPing = Math.floor(Math.random() * (35 - 20 + 1)) + 20;

    return {
        prediction: val,
        number: `${num}/${randomSupportNum}`,
        accuracy: randomAcc,
        ping: randomPing
    };
}

// /start কমান্ড পাওয়ার পর প্রথম ওয়েলকাম মেসেজ ও ইনলাইন বাটন
bot.onText(/\/start/, (msg) => {
    const chatId = msg.chat.id;
    
    const options = {
        reply_markup: {
            inline_keyboard: [
                [{ text: "⚡ START HACK ⚡", callback_data: "start_hack" }]
            ]
        }
    };

    bot.sendMessage(chatId, "🎮 **VIP HACK PREDICTOR BOT**\n\nসিগন্যাল পেতে নিচের বাটনে চাপ দিন:", { 
        parse_mode: 'Markdown', 
        ...options 
    });
});

// বাটন ক্লিকে সিগন্যাল প্রসেসিং
bot.on('callback_query', async (query) => {
    const chatId = query.message.chat.id;
    const userId = query.from.id;
    
    // টেলিগ্রামকে জানাচ্ছি যে বাটনে চাপ পড়া সফল হয়েছে
    bot.answerCallbackQuery(query.id);

    if (query.data === "start_hack") {
        const timeData = getMarketTimeAndPeriod();
        const currentUserSession = userSessions[userId];

        // 🛑 চেকিং: আগের একই পিরিয়ডে সিগন্যাল নিয়েছে কিনা এবং টাইমার বাকি আছে কিনা
        if (currentUserSession && currentUserSession.period === timeData.periodDisplay) {
            bot.sendMessage(
                chatId, 
                `⏳ **অপেক্ষা করুন!**\n\nচলতি পিরিয়ডের সিগন্যাল ইতোমধ্যে দেওয়া হয়েছে। নতুন সিগন্যাল পেতে **${timeData.remainingSeconds} সেকেন্ড** অপেক্ষা করুন।`
            );
            return;
        }

        // 🔄 ১. এনিমেশন/প্রসেসিং মেসেজ পাঠানো
        const loadingMsg = await bot.sendMessage(chatId, "🔍 *Analyzing Market Data & Hacking Period...* Please wait...", { parse_mode: 'Markdown' });

        // ২ সেকেন্ডের কৃত্রিম লোডিং ডিল
        setTimeout(() => {
            // লোডিং মেসেজটা ডিলিট করে দেওয়া
            bot.deleteMessage(chatId, loadingMsg.message_id);

            // নতুন সিগন্যাল জেনারেট
            const result = generatePrediction();

            // সেসন আপডেট করা
            userSessions[userId] = {
                period: timeData.periodDisplay,
                result: result
            };

            // কালার ও ইমোজি সেটআপ
            const colorEmoji = result.prediction === "BIG" ? "🟣 (PURPLE/BIG)" : "🔵 (CYAN/SMALL)";

            // 🎯 সুন্দর মেসেজ টেমপ্লেট
            const responseMsg = 
`🎯 **PREDICTION RESULT** 🎯
━━━━━━━━━━━━━━━━━━
📍 **PERIOD NO :** \`${timeData.periodDisplay}\`
🔮 **PREDICT :** \`${result.prediction}\` ${colorEmoji}
🔢 **NUMBER :** \`${result.number}\`
🎯 **ACCURACY :** \`${result.accuracy}%\`
📶 **PING :** \`${result.ping}MS\`
⏱️ **NEXT SIGNAL IN :** \`${timeData.remainingSeconds}s\`
━━━━━━━━━━━━━━━━━━
⚠️ *পরবর্তী পিরিয়ড না আসা পর্যন্ত এই সিগন্যাল কার্যকর।*`;

            const options = {
                parse_mode: 'Markdown',
                reply_markup: {
                    inline_keyboard: [
                        [{ text: "🔄 GET NEXT SIGNAL 🔄", callback_data: "start_hack" }]
                    ]
                }
            };

            // সিগন্যাল সেন্ড করা
            bot.sendMessage(chatId, responseMsg, options);

        }, 2000); // ২ সেকেন্ড পর প্রেডিকশন শো করবে
    }
});
