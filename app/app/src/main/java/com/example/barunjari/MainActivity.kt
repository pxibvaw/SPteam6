package com.example.barunjari

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.shadow
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.ColorFilter
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.text.SpanStyle
import androidx.compose.ui.text.buildAnnotatedString
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.withStyle
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.delay

private val Green = Color(0xFF16A34A)
private val PaleGreen = Color(0xFFE7F6EC)
private val Mint = Color(0xFFF2FAF5)
private val Ink = Color(0xFF111111)
private val Slate = Color(0xFF374151)
private val Muted = Color(0xFF6B7280)
private val Soft = Color(0xFF9CA3AF)
private val Background = Color(0xFFF5F6F6)
private val Line = Color(0xFFE5E7EB)
private val Danger = Color(0xFFE85050)

private enum class Page { ONBOARDING, GOALS, CALIBRATION, HOME, REPORT, FEEDBACK, SETTINGS, DEVICE }
private val goalNames = listOf("다리 꼬기 줄이기", "목 앞으로 내미는 자세 줄이기", "한쪽으로 기대는 습관 줄이기", "장시간 연속 착석 줄이기")
private val goalDescriptions = listOf("비대칭 착석(다리 꼬기 추정) 시간을 추적해요", "목 전방 자세가 이어진 시간을 추적해요", "좌우 체중 편향 시간을 비교해요", "최대 연속 착석시간을 알려드려요")
private val goalIcons = listOf(R.drawable.legs, R.drawable.user, R.drawable.balance, R.drawable.clock)

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val prefs = getSharedPreferences("sitsense", MODE_PRIVATE)
        setContent {
            SitSenseApp(
                firstRunDone = prefs.getBoolean("first_run_done", false),
                savedGoals = prefs.getString("goals", "0,1") ?: "0,1",
                saveOnboarding = { prefs.edit().putBoolean("first_run_done", true).apply() },
                saveGoals = { selected -> prefs.edit().putString("goals", selected.sorted().joinToString(",")).apply() },
                resetAll = { prefs.edit().clear().apply() }
            )
        }
    }
}

@Composable
private fun SitSenseApp(
    firstRunDone: Boolean,
    savedGoals: String,
    saveOnboarding: () -> Unit,
    saveGoals: (Set<Int>) -> Unit,
    resetAll: () -> Unit
) {
    var page by rememberSaveable { mutableStateOf(if (firstRunDone) Page.HOME else Page.ONBOARDING) }
    var onboardingComplete by rememberSaveable { mutableStateOf(firstRunDone) }
    var goals by rememberSaveable { mutableStateOf(savedGoals.split(",").mapNotNull(String::toIntOrNull).toSet()) }
    var editingGoals by rememberSaveable { mutableStateOf(false) }
    var connected by rememberSaveable { mutableStateOf(false) }
    var alerts by rememberSaveable { mutableStateOf(true) }
    var standAlert by rememberSaveable { mutableStateOf(true) }
    var threshold by rememberSaveable { mutableIntStateOf(5) }
    var standMinutes by rememberSaveable { mutableIntStateOf(50) }
    var weekly by rememberSaveable { mutableStateOf(false) }
    var dateOffset by rememberSaveable { mutableIntStateOf(0) }
    var showAlertCard by rememberSaveable { mutableStateOf(true) }
    var dialog by rememberSaveable { mutableStateOf("") }

    MaterialTheme(colorScheme = lightColorScheme(primary = Green, background = Background, surface = Color.White)) {
        Column(Modifier.fillMaxSize().background(Background).windowInsetsPadding(WindowInsets.safeDrawing)) {
            when (page) {
                Page.ONBOARDING -> OnboardingScreen { page = Page.GOALS }
                Page.GOALS -> GoalsScreen(
                    selected = goals,
                    editing = editingGoals,
                    onBack = { page = if (editingGoals) Page.SETTINGS else Page.ONBOARDING },
                    onToggle = { index ->
                        goals = if (index in goals) goals - index else if (goals.size < 2) goals + index else goals
                    },
                    onNext = {
                        saveGoals(goals)
                        page = if (editingGoals) Page.SETTINGS else Page.CALIBRATION
                        editingGoals = false
                    }
                )
                Page.CALIBRATION -> CalibrationScreen(
                    onBack = { page = if (onboardingComplete) Page.SETTINGS else Page.GOALS },
                    onDone = { saveOnboarding(); onboardingComplete = true; page = Page.HOME }
                )
                else -> {
                    Box(Modifier.weight(1f)) {
                        when (page) {
                            Page.HOME -> HomeScreen(goals, showAlertCard, { showAlertCard = false }, { page = Page.FEEDBACK })
                            Page.REPORT -> ReportScreen(weekly, { weekly = it; dateOffset = 0 }, dateOffset, { dateOffset += it })
                            Page.FEEDBACK -> FeedbackScreen(goals, { editingGoals = true; page = Page.GOALS })
                            Page.SETTINGS -> SettingsScreen(
                                connected, goals, alerts, { alerts = it }, standAlert, { standAlert = it },
                                threshold, standMinutes,
                                onAction = { action ->
                                    when (action) {
                                        "기기 재연결" -> page = Page.DEVICE
                                        "개선 목표" -> { editingGoals = true; page = Page.GOALS }
                                        "기준 자세 다시 측정" -> page = Page.CALIBRATION
                                        else -> dialog = action
                                    }
                                }
                            )
                            Page.DEVICE -> DeviceScreen(connected, { connected = !connected })
                            else -> Unit
                        }
                    }
                    BottomBar(page, onSelect = { page = it })
                }
            }
        }
        if (dialog.isNotEmpty()) {
            AlertDialog(
                onDismissRequest = { dialog = "" },
                title = { Text(dialog) },
                text = {
                    when (dialog) {
                        "알림 기준" -> ChoiceColumn(listOf(3, 5, 10).map { "$it 분 이상 지속 시" }, threshold) { threshold = it }
                        "일어나기 알림" -> ChoiceColumn(listOf(30, 50, 60).map { "$it 분 연속 착석 시" }, standMinutes) { standMinutes = it }
                        "기록 내보내기" -> Text("현재 화면에는 예시 데이터가 표시되고 있어요. 실제 센서 기록이 연결되면 CSV 내보내기를 사용할 수 있어요.")
                        "기록 초기화" -> Text("선택한 목표와 온보딩 설정을 지우고 처음 화면으로 돌아갈까요?")
                        else -> Text("연결된 센서가 준비되면 사용할 수 있어요.")
                    }
                },
                confirmButton = { TextButton(onClick = {
                    if (dialog == "기록 초기화") {
                        resetAll()
                        goals = setOf(0, 1)
                        onboardingComplete = false
                        connected = false
                        page = Page.ONBOARDING
                    }
                    dialog = ""
                }) { Text(if (dialog == "기록 초기화") "초기화" else "확인") } },
                dismissButton = {
                    if (dialog == "알림 기준" || dialog == "일어나기 알림" || dialog == "기록 초기화") {
                        TextButton(onClick = { dialog = "" }) { Text("취소") }
                    }
                }
            )
        }
    }
}

@Composable
private fun ChoiceColumn(labels: List<String>, selected: Int, onSelect: (Int) -> Unit) {
    Column {
        labels.forEach { label ->
            val value = label.substringBefore(' ').toInt()
            Row(Modifier.fillMaxWidth().clickable { onSelect(value) }.padding(vertical = 10.dp), verticalAlignment = Alignment.CenterVertically) {
                RadioButton(selected == value, onClick = { onSelect(value) })
                Text(label)
            }
        }
    }
}

@Composable
private fun Label(text: String, size: Int = 14, color: Color = Ink, weight: FontWeight = FontWeight.Normal, modifier: Modifier = Modifier, align: TextAlign? = null) {
    Text(text, modifier, color = color, fontSize = size.sp, fontWeight = weight, lineHeight = (size * 1.42f).sp, textAlign = align)
}

@Composable
private fun Icon(id: Int, modifier: Modifier = Modifier, tint: Color = Slate) {
    IconImage(id, modifier, tint)
}

@Composable
private fun IconImage(id: Int, modifier: Modifier = Modifier, tint: Color? = null) {
    androidx.compose.foundation.Image(
        painter = painterResource(id), contentDescription = null, modifier = modifier,
        colorFilter = tint?.let { ColorFilter.tint(it) }
    )
}

@Composable
private fun RoundedCard(modifier: Modifier = Modifier, padding: Int = 18, content: @Composable ColumnScope.() -> Unit) {
    Column(modifier.shadow(8.dp, RoundedCornerShape(22.dp), ambientColor = Color.Black.copy(alpha = .05f))
        .clip(RoundedCornerShape(22.dp)).background(Color.White).padding(padding.dp), content = content)
}

@Composable
private fun Pill(text: String, green: Boolean = true, modifier: Modifier = Modifier) {
    Box(modifier.clip(CircleShape).background(if (green) PaleGreen else Color(0xFFF3F4F6)).padding(horizontal = 10.dp, vertical = 5.dp)) {
        Label(text, 11, if (green) Green else Slate, FontWeight.SemiBold)
    }
}

@Composable
private fun PrimaryButton(text: String, onClick: () -> Unit, modifier: Modifier = Modifier, enabled: Boolean = true, color: Color = Ink) {
    Box(modifier.fillMaxWidth().height(56.dp).clip(RoundedCornerShape(16.dp))
        .background(if (enabled) color else Line).clickable(enabled = enabled, onClick = onClick), contentAlignment = Alignment.Center) {
        Label(text, 16, if (enabled) Color.White else Soft, FontWeight.Bold)
    }
}

@Composable
private fun BottomBar(page: Page, onSelect: (Page) -> Unit) {
    Row(Modifier.fillMaxWidth().height(72.dp).background(Color.White).padding(top = 8.dp), horizontalArrangement = Arrangement.SpaceEvenly) {
        listOf(
            Triple(Page.HOME, "홈", R.drawable.home),
            Triple(Page.REPORT, "리포트", R.drawable.chart),
            Triple(Page.FEEDBACK, "피드백", R.drawable.bulb),
            Triple(Page.SETTINGS, "설정", R.drawable.sliders)
        ).forEach { (target, title, icon) ->
            val active = if (page == Page.DEVICE) target == Page.SETTINGS else target == page
            Column(Modifier.weight(1f).fillMaxHeight().clickable { onSelect(target) }, horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.Top) {
                Icon(icon, Modifier.size(23.dp), if (active) Green else Soft)
                Spacer(Modifier.height(3.dp))
                Label(title, 11, if (active) Green else Soft, if (active) FontWeight.Bold else FontWeight.Normal)
            }
        }
    }
}

@Composable
private fun OnboardingScreen(onStart: () -> Unit) {
    Box(Modifier.fillMaxSize().background(Color.White)) {
        Column(Modifier.fillMaxSize().padding(horizontal = 20.dp).padding(top = 12.dp, bottom = 26.dp)) {
            Illustration(Modifier.fillMaxWidth().height(330.dp))
            Spacer(Modifier.height(26.dp))
            Text(buildAnnotatedString {
                append("앉아만 있어도\n자세 습관이 ")
                withStyle(SpanStyle(color = Green)) { append("기록돼요") }
            }, fontSize = 28.sp, fontWeight = FontWeight.Bold, lineHeight = 38.sp, color = Ink)
            Spacer(Modifier.height(14.dp))
            Label("카메라와 의자 좌면 압력센서로 상체와 하체 자세를 함께 분석하고, 하루·한 주의 자세 습관을 리포트로 보여드려요.", 15, Muted)
            Spacer(Modifier.height(20.dp))
            Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                listOf("상체 분석", "착석·체중 분포", "영상 저장 안 함").forEach { Pill(it, green = false) }
            }
            Spacer(Modifier.weight(1f))
            PrimaryButton("시작하기", onStart)
            Spacer(Modifier.height(22.dp))
            Label("평소 쓰는 의자에서 착용 장비 없이 사용해요", 12, Soft, modifier = Modifier.fillMaxWidth(), align = TextAlign.Center)
        }
    }
}

@Composable
private fun Illustration(modifier: Modifier = Modifier) {
    Box(modifier.clip(RoundedCornerShape(28.dp)).background(PaleGreen)) {
        Box(Modifier.size(250.dp).align(Alignment.Center).clip(CircleShape).background(Color.White.copy(alpha = .67f)))
        Box(Modifier.offset(x = 99.dp, y = 150.dp).size(16.dp, 132.dp).clip(RoundedCornerShape(8.dp)).background(Color(0xFFCBD2D9)))
        Box(Modifier.offset(x = 120.dp, y = 277.dp).size(8.dp, 45.dp).background(Color(0xFFCBD2D9)))
        Box(Modifier.offset(x = 226.dp, y = 277.dp).size(8.dp, 45.dp).background(Color(0xFFCBD2D9)))
        Box(Modifier.offset(x = 130.dp, y = 157.dp).size(90.dp, 112.dp).clip(RoundedCornerShape(topStart = 40.dp, topEnd = 40.dp, bottomStart = 22.dp, bottomEnd = 22.dp)).background(Green))
        Box(Modifier.offset(x = 149.dp, y = 96.dp).size(52.dp).clip(CircleShape).background(Ink))
        Box(Modifier.offset(x = 96.dp, y = 256.dp).size(160.dp, 24.dp).clip(CircleShape).background(Color.White).border(1.dp, Color(0xFFCBD2D9), CircleShape)) {
            Row(Modifier.fillMaxSize(), verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.SpaceEvenly) {
                repeat(4) { Box(Modifier.size(9.dp).clip(CircleShape).background(Green)) }
            }
        }
        Row(Modifier.offset(x = 18.dp, y = 22.dp).shadow(5.dp, RoundedCornerShape(16.dp)).clip(RoundedCornerShape(16.dp)).background(Color.White).padding(12.dp), verticalAlignment = Alignment.CenterVertically) {
            ProgressRing(72, Modifier.size(34.dp))
            Spacer(Modifier.width(9.dp))
            Column { Label("정상 자세", 11, Muted); Label("72%", 16, Ink, FontWeight.Bold) }
        }
        Box(Modifier.offset(x = 262.dp, y = 103.dp).size(42.dp).clip(RoundedCornerShape(12.dp)).background(Ink), contentAlignment = Alignment.Center) {
            Icon(R.drawable.camera, Modifier.size(20.dp), Color.White)
        }
        Row(Modifier.offset(x = 196.dp, y = 286.dp).clip(RoundedCornerShape(14.dp)).background(Color.White).padding(horizontal = 12.dp, vertical = 8.dp), verticalAlignment = Alignment.CenterVertically) {
            Icon(R.drawable.seat, Modifier.size(16.dp), Green)
            Spacer(Modifier.width(8.dp))
            Label("좌 49 : 우 51", 12, Ink, FontWeight.Bold)
        }
    }
}

@Composable
private fun ProgressRing(percent: Int, modifier: Modifier = Modifier, color: Color = Green, base: Color = PaleGreen, stroke: Float = 8f) {
    Canvas(modifier) {
        drawArc(base, -90f, 360f, false, style = Stroke(stroke.dp.toPx(), cap = StrokeCap.Butt))
        drawArc(color, -90f, 360f * percent / 100f, false, style = Stroke(stroke.dp.toPx(), cap = StrokeCap.Butt))
    }
}

@Composable
private fun StepHeader(step: Int, onBack: () -> Unit) {
    Row(Modifier.fillMaxWidth().height(30.dp), verticalAlignment = Alignment.CenterVertically) {
        Box(Modifier.size(28.dp).clickable(onClick = onBack), contentAlignment = Alignment.CenterStart) {
            Icon(R.drawable.chevl, Modifier.size(24.dp), Ink)
        }
        Spacer(Modifier.weight(1f))
        Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
            repeat(2) { index -> Box(Modifier.size(28.dp, 4.dp).clip(CircleShape).background(if (index < step) Green else Line)) }
        }
        Spacer(Modifier.weight(1f))
        Label("$step / 2", 12, Muted)
    }
}

@Composable
private fun GoalsScreen(selected: Set<Int>, editing: Boolean, onBack: () -> Unit, onToggle: (Int) -> Unit, onNext: () -> Unit) {
    Column(Modifier.fillMaxSize().background(Color.White).padding(horizontal = 20.dp).padding(top = 8.dp, bottom = 26.dp)) {
        if (editing) Row(Modifier.height(34.dp).clickable(onClick = onBack), verticalAlignment = Alignment.CenterVertically) { Icon(R.drawable.chevl, Modifier.size(24.dp), Ink); Spacer(Modifier.width(8.dp)); Label("목표 수정", 18, Ink, FontWeight.Bold) }
        else StepHeader(1, onBack)
        Spacer(Modifier.height(22.dp))
        Label("어떤 습관을\n가장 줄이고 싶나요?", 25, Ink, FontWeight.Bold)
        Spacer(Modifier.height(10.dp))
        Label("선택한 목표와 관련된 지표를 리포트에서 먼저 보여드려요. 최대 두 개 선택할 수 있어요.", 14, Muted)
        Spacer(Modifier.height(24.dp))
        goalNames.forEachIndexed { index, name ->
            val chosen = index in selected
            Row(Modifier.fillMaxWidth().padding(bottom = 12.dp).height(79.dp)
                .clip(RoundedCornerShape(18.dp)).background(if (chosen) Mint else Color.White)
                .border(if (chosen) 1.5.dp else 1.dp, if (chosen) Green else Line, RoundedCornerShape(18.dp))
                .clickable { onToggle(index) }.padding(horizontal = 16.dp), verticalAlignment = Alignment.CenterVertically) {
                Box(Modifier.size(44.dp).clip(RoundedCornerShape(13.dp)).background(if (chosen) Green else Color(0xFFF3F4F6)), contentAlignment = Alignment.Center) {
                    Icon(goalIcons[index], Modifier.size(22.dp), if (chosen) Color.White else Slate)
                }
                Spacer(Modifier.width(14.dp))
                Column(Modifier.weight(1f)) {
                    Label(name, 14, Ink, FontWeight.Bold)
                    Label(goalDescriptions[index], 11, Muted)
                }
                Box(Modifier.size(24.dp).border(1.5.dp, if (chosen) Green else Color(0xFFCBD2D9), CircleShape).clip(CircleShape).background(if (chosen) Green else Color.White), contentAlignment = Alignment.Center) {
                    if (chosen) Icon(R.drawable.check, Modifier.size(14.dp), Color.White)
                }
            }
        }
        Spacer(Modifier.weight(1f))
        Label("${selected.size}개 선택됨 · 설정에서 언제든 바꿀 수 있어요", 12, Muted, modifier = Modifier.fillMaxWidth(), align = TextAlign.Center)
        Spacer(Modifier.height(26.dp))
        PrimaryButton(if (editing) "저장" else "다음", onNext, enabled = selected.isNotEmpty())
    }
}

@Composable
private fun CalibrationScreen(onBack: () -> Unit, onDone: () -> Unit) {
    var seconds by remember { mutableIntStateOf(10) }
    LaunchedEffect(Unit) {
        while (seconds > 0) { delay(1000); seconds-- }
    }
    Column(Modifier.fillMaxSize().background(Color.White).padding(horizontal = 20.dp).padding(top = 8.dp, bottom = 26.dp)) {
        StepHeader(2, onBack)
        Spacer(Modifier.height(22.dp))
        Label("바른 자세로\n편하게 앉아주세요", 25, Ink, FontWeight.Bold)
        Spacer(Modifier.height(10.dp))
        Label("처음 10초 동안의 자세를 기준으로 저장하고, 이후에는 이 기준과 비교해 변화를 기록해요.", 14, Muted)
        Spacer(Modifier.height(36.dp))
        Box(Modifier.fillMaxWidth().height(210.dp), contentAlignment = Alignment.TopCenter) {
            Box(Modifier.size(168.dp), contentAlignment = Alignment.Center) {
                ProgressRing((10 - seconds) * 10, Modifier.fillMaxSize(), stroke = 12f)
                Column(horizontalAlignment = Alignment.CenterHorizontally) {
                    Label("$seconds", 54, Ink, FontWeight.Bold)
                    Label("초 남음", 13, Muted)
                }
            }
            Pill(if (seconds > 0) "● 기준 자세 측정 중" else "● 측정 완료", modifier = Modifier.align(Alignment.BottomCenter))
        }
        Spacer(Modifier.height(16.dp))
        RoundedCard(Modifier.fillMaxWidth().background(Color(0xFFF3F4F6), RoundedCornerShape(18.dp)), padding = 0) {
            listOf(Triple("카메라", "상체 인식됨", R.drawable.camera), Triple("좌면 압력센서", "6개 인식됨", R.drawable.seat), Triple("거리센서", "거리 인식됨", R.drawable.distence), Triple("착석 감지", "앉아 있음", R.drawable.user)).forEachIndexed { index, item ->
                Row(Modifier.fillMaxWidth().height(45.dp).background(Color(0xFFF3F4F6)).padding(horizontal = 16.dp), verticalAlignment = Alignment.CenterVertically) {
                    Icon(item.third, Modifier.size(20.dp), Slate)
                    Spacer(Modifier.width(12.dp))
                    Label(item.first, 13, Ink, FontWeight.SemiBold, Modifier.weight(1f))
                    Label(item.second, 12, Soft)
                    Spacer(Modifier.width(5.dp))
                    Icon(R.drawable.check, Modifier.size(15.dp), Soft)
                }
                if (index < 3) HorizontalDivider(Modifier.padding(horizontal = 16.dp), color = Line)
            }
        }
        Spacer(Modifier.height(20.dp))
        Label("◇ 영상은 기기 안에서 관절 좌표만 계산한 뒤 바로 삭제돼요", 11, Muted)
        Spacer(Modifier.weight(1f))
        PrimaryButton(if (seconds > 0) "측정 중…" else "완료하고 홈으로", onDone, enabled = seconds == 0)
        Spacer(Modifier.height(8.dp))
        Label("현재는 센서 입력 없이 진행되는 화면 체험이에요", 11, Soft, modifier = Modifier.fillMaxWidth(), align = TextAlign.Center)
    }
}

@Composable
private fun HomeScreen(goals: Set<Int>, alertVisible: Boolean, dismissAlert: () -> Unit, openFeedback: () -> Unit) {
    LazyColumn(Modifier.fillMaxSize().background(Background), contentPadding = PaddingValues(start = 18.dp, end = 18.dp, top = 12.dp, bottom = 24.dp), verticalArrangement = Arrangement.spacedBy(14.dp)) {
        item {
            Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                Column(Modifier.weight(1f)) {
                    Label("9월 27일 일요일", 12, Muted)
                    Label("좋은 오후예요, 송이님", 20, Ink, FontWeight.Bold)
                }
                Box(Modifier.size(42.dp).clip(CircleShape).background(Color.White).clickable(onClick = openFeedback), contentAlignment = Alignment.Center) {
                    Icon(R.drawable.bell, Modifier.size(20.dp), Ink)
                }
            }
        }
        item {
            RoundedCard(Modifier.fillMaxWidth(), padding = 18) {
                Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                    Column(Modifier.weight(1f)) {
                        Pill("● 하루 평균 자세")
                        Spacer(Modifier.height(4.dp))
                        Label("목이 앞으로 기울었어요.", 11, Ink)
                        Label("목 전방 자세", 20, Ink, FontWeight.Bold)
                        Label("오른쪽 편향 비율 · 33%", 12, Slate)
                    }
                    IconImage(R.drawable.plant, Modifier.size(100.dp, 97.dp))
                }
            }
        }
        item {
            Row(Modifier.fillMaxWidth().clip(RoundedCornerShape(22.dp)).background(Green).padding(18.dp), verticalAlignment = Alignment.CenterVertically) {
                Column(Modifier.weight(1f)) {
                    Pill("● 착석 중 · 42분째", modifier = Modifier)
                    Spacer(Modifier.height(8.dp))
                    Label("오늘 정상 자세 비율", 12, Color.White.copy(alpha = .85f))
                    Label("72%", 39, Color.White, FontWeight.Bold)
                    Label("총 착석 4시간 12분 · 정상 3시간 1분", 11, Color.White)
                }
                ProgressRing(72, Modifier.size(82.dp), Color.White, Color.White.copy(alpha = .25f), 9f)
            }
        }
        if (alertVisible) item {
            Row(Modifier.fillMaxWidth().clip(RoundedCornerShape(18.dp)).background(Ink).padding(14.dp), verticalAlignment = Alignment.CenterVertically) {
                Box(Modifier.size(38.dp).clip(RoundedCornerShape(12.dp)).background(Color.White.copy(alpha = .1f)), contentAlignment = Alignment.Center) {
                    Icon(R.drawable.alert, Modifier.size(19.dp), Green)
                }
                Spacer(Modifier.width(10.dp))
                Column(Modifier.weight(1f)) {
                    Label("목 전방 자세가 5분째 이어지고 있어요", 11, Color.White, FontWeight.Bold)
                    Label("목과 어깨를 가볍게 움직여 보세요", 11, Soft)
                }
                Box(Modifier.size(24.dp).clickable(onClick = dismissAlert), contentAlignment = Alignment.Center) { Icon(R.drawable.x, Modifier.size(17.dp), Soft) }
            }
        }
        item {
            Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                Label("지금 자세", 15, Ink, FontWeight.Bold, Modifier.weight(1f))
                Label("상체·하체를 따로 기록해요", 10, Soft)
            }
        }
        item {
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                RoundedCard(Modifier.weight(1f).height(210.dp), padding = 14) {
                    Label("▣ 거리센서 · 카메라", 10, Muted)
                    Spacer(Modifier.height(8.dp))
                    Label("● 정상", 16, Ink, FontWeight.Bold)
                    Spacer(Modifier.height(8.dp))
                    Box(Modifier.fillMaxWidth().height(84.dp).clip(RoundedCornerShape(14.dp)).background(Background), contentAlignment = Alignment.Center) {
                        Icon(R.drawable.user, Modifier.size(47.dp), Green)
                    }
                    Spacer(Modifier.height(5.dp))
                    Label("어깨 기울기 1.2°\n목 위치 기준 범위 안", 10, Soft)
                }
                RoundedCard(Modifier.weight(1f).height(210.dp), padding = 14) {
                    Label("♧ 좌면 · 압력센서", 10, Muted)
                    Spacer(Modifier.height(8.dp))
                    Label("● 오른쪽 편향", 16, Ink, FontWeight.Bold)
                    Spacer(Modifier.height(8.dp))
                    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(4.dp)) {
                        listOf(16, 18, 20).forEach { PressureCell(it, Modifier.weight(1f)) }
                    }
                    Spacer(Modifier.height(4.dp))
                    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(4.dp)) {
                        listOf(22, 25, 33).forEach { PressureCell(it, Modifier.weight(1f)) }
                    }
                    Spacer(Modifier.height(8.dp))
                    Label("앞·뒤 / 좌·우 압력 비율\nFSR 6개 기준", 10, Soft)
                }
            }
        }
        item {
            RoundedCard(Modifier.fillMaxWidth(), padding = 16) {
                Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                    Label("내 목표", 15, Ink, FontWeight.Bold, Modifier.weight(1f))
                    Label("피드백 보기 ›", 11, Green, FontWeight.Bold, Modifier.clickable(onClick = openFeedback))
                }
                Spacer(Modifier.height(7.dp))
                goals.sorted().forEachIndexed { order, index ->
                    Row(Modifier.fillMaxWidth().padding(vertical = 8.dp), verticalAlignment = Alignment.CenterVertically) {
                        Box(Modifier.size(35.dp).clip(RoundedCornerShape(10.dp)).background(Background), contentAlignment = Alignment.Center) { Icon(goalIcons[index], Modifier.size(20.dp)) }
                        Spacer(Modifier.width(10.dp))
                        Column(Modifier.weight(1f)) {
                            Label(goalNames[index], 12, Ink, FontWeight.Bold)
                            Label(if (index == 0) "오늘 25분 · 어제 38분" else if (index == 1) "오늘 42분 · 어제 51분" else "오늘 기록 중", 10, Muted)
                        }
                        Pill(if (index == 0) "↓ 13분" else "↓ 9분")
                    }
                    if (order < goals.size - 1) HorizontalDivider(color = Line)
                }
            }
        }
        item { Label("화면의 자세 수치는 예시 데이터예요", 10, Soft, modifier = Modifier.fillMaxWidth(), align = TextAlign.Center) }
    }
}

@Composable
private fun PressureCell(value: Int, modifier: Modifier = Modifier) {
    val shade = when (value) { 33 -> Green; 25 -> Color(0xFF77CAA0); else -> Color(0xFFA9DFC0) }
    Box(modifier.height(32.dp).clip(RoundedCornerShape(8.dp)).background(shade), contentAlignment = Alignment.Center) {
        Label("$value%", 11, if (value >= 25) Color.White else Color(0xFF087B36), FontWeight.Bold)
    }
}

@Composable
private fun ReportScreen(weekly: Boolean, setWeekly: (Boolean) -> Unit, dateOffset: Int, changeDate: (Int) -> Unit) {
    LazyColumn(Modifier.fillMaxSize().background(Background), contentPadding = PaddingValues(start = 16.dp, end = 16.dp, top = 13.dp, bottom = 20.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
        item {
            Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                Label("리포트", 21, Ink, FontWeight.Bold, Modifier.weight(1f))
                Box(Modifier.size(38.dp).clip(CircleShape).background(Color.White), contentAlignment = Alignment.Center) { Icon(R.drawable.calendar, Modifier.size(20.dp), Ink) }
            }
        }
        item {
            Row(Modifier.fillMaxWidth().height(46.dp).clip(RoundedCornerShape(13.dp)).background(Color(0xFFE9EBEA)).padding(4.dp)) {
                listOf(false to "일간", true to "주간").forEach { (mode, title) ->
                    Box(Modifier.weight(1f).fillMaxHeight().clip(RoundedCornerShape(10.dp)).background(if (weekly == mode) Color.White else Color.Transparent).clickable { setWeekly(mode) }, contentAlignment = Alignment.Center) {
                        Label(title, 12, if (weekly == mode) Ink else Muted, if (weekly == mode) FontWeight.Bold else FontWeight.Normal)
                    }
                }
            }
        }
        item {
            Row(Modifier.fillMaxWidth().height(34.dp), verticalAlignment = Alignment.CenterVertically) {
                Box(Modifier.size(32.dp).clip(CircleShape).background(Color.White).clickable { changeDate(-1) }, contentAlignment = Alignment.Center) { Icon(R.drawable.chevl, Modifier.size(16.dp)) }
                Label(if (dateOffset == 0) { if (weekly) "9월 21일 – 27일" else "9월 27일 (일)" } else if (weekly) "${dateOffset}주 전" else "${-dateOffset}일 전", 13, Ink, FontWeight.Bold, Modifier.weight(1f), TextAlign.Center)
                Box(Modifier.size(32.dp).clip(CircleShape).background(Color.White).clickable { if (dateOffset < 0) changeDate(1) }, contentAlignment = Alignment.Center) { Icon(R.drawable.chevr, Modifier.size(16.dp), if (dateOffset < 0) Slate else Soft) }
            }
        }
        if (weekly) {
            item { WeeklySummary() }
            item { WeeklyChart() }
            item { WeeklyComparison() }
            item { DarkInsight("이번 주 패턴", "오후 3시 이후에 자세가 가장 자주 무너졌어요") }
        } else {
            item {
                Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        MetricCard("총 착석시간", "4시간 12분", Modifier.weight(1f))
                        MetricCard("정상 자세", "72%", Modifier.weight(1f), Green)
                    }
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        MetricCard("최대 연속 착석", "1시간 40분", Modifier.weight(1f))
                        MetricCard("자세 무너진 시점", "평균 29분 후", Modifier.weight(1f))
                    }
                }
            }
            item {
                RoundedCard(Modifier.fillMaxWidth(), padding = 15) {
                    Row { Label("자세별 누적시간", 14, Ink, FontWeight.Bold, Modifier.weight(1f)); Label("착석 4시간 12분 기준", 10, Soft) }
                    Spacer(Modifier.height(12.dp))
                    listOf(Triple("정상 자세", "3시간 1분", .9f), Triple("목 전방 자세", "42분", .19f), Triple("비대칭 착석 (다리 꼬기 추정)", "25분", .11f), Triple("오른쪽 체중 편향", "22분", .1f), Triple("왼쪽 체중 편향", "8분", .04f)).forEachIndexed { i, row ->
                        Row { Label(row.first, 11, Slate, modifier = Modifier.weight(1f)); Label(row.second, 11, Ink, FontWeight.Bold) }
                        Spacer(Modifier.height(5.dp))
                        RatioBar(row.third, if (i == 0) Green else if (i == 1) Ink else Soft)
                        Spacer(Modifier.height(9.dp))
                    }
                }
            }
            item {
                RoundedCard(Modifier.fillMaxWidth(), padding = 16) {
                    Label("좌우 체중 편향 비교", 14, Ink, FontWeight.Bold)
                    Spacer(Modifier.height(11.dp))
                    Row { Column(Modifier.weight(1f)) { Label("왼쪽", 10, Muted); Label("8분", 17, Slate, FontWeight.Bold) }; Column(horizontalAlignment = Alignment.End) { Label("오른쪽", 10, Muted); Label("22분", 17, Ink, FontWeight.Bold) } }
                    Spacer(Modifier.height(9.dp))
                    Row(Modifier.fillMaxWidth().height(10.dp), horizontalArrangement = Arrangement.spacedBy(3.dp)) {
                        Box(Modifier.weight(.27f).fillMaxHeight().clip(RoundedCornerShape(5.dp)).background(Color(0xFFCBD0D4)))
                        Box(Modifier.weight(.73f).fillMaxHeight().clip(RoundedCornerShape(5.dp)).background(Ink))
                    }
                    Spacer(Modifier.height(10.dp))
                    Label("ⓘ 오른쪽으로 기댄 시간이 왼쪽보다 약 2.8배 길어요", 11, Slate)
                }
            }
            item {
                RoundedCard(Modifier.fillMaxWidth(), padding = 16) {
                    Label("화면과의 거리", 14, Ink, FontWeight.Bold)
                    Spacer(Modifier.height(9.dp))
                    Row { Column(Modifier.weight(1f)) { Label("평균 거리", 10, Muted); Label("52cm", 18, Slate, FontWeight.Bold) }; Column(horizontalAlignment = Alignment.End) { Label("가까웠던 시간", 10, Muted); Label("38분", 18, Ink, FontWeight.Bold) } }
                    Spacer(Modifier.height(9.dp))
                    RatioBar(.84f, Color(0xFFCBD0D4))
                    Spacer(Modifier.height(9.dp))
                    Label("ⓘ 기준보다 10cm 이상 가까웠던 시간이 38분이에요", 11, Slate)
                }
            }
            item {
                RoundedCard(Modifier.fillMaxWidth(), padding = 17) {
                    Label("오늘 가장 많이 나타난 습관", 11, Muted)
                    Label("목 전방 자세", 17, Ink, FontWeight.Bold)
                    Label("42분", 21, Green, FontWeight.Bold)
                }
            }
        }
        item { Label("현재 리포트 수치는 예시 데이터예요", 10, Soft, modifier = Modifier.fillMaxWidth(), align = TextAlign.Center) }
    }
}

@Composable
private fun MetricCard(title: String, value: String, modifier: Modifier = Modifier, valueColor: Color = Ink) {
    RoundedCard(modifier.height(58.dp), padding = 11) {
        Label(title, 10, Muted)
        Label(value, 15, valueColor, FontWeight.Bold)
    }
}

@Composable
private fun RatioBar(ratio: Float, color: Color) {
    Box(Modifier.fillMaxWidth().height(6.dp).clip(CircleShape).background(Color(0xFFF0F1F2))) {
        Box(Modifier.fillMaxWidth(ratio).fillMaxHeight().clip(CircleShape).background(color))
    }
}

@Composable
private fun WeeklySummary() {
    RoundedCard(Modifier.fillMaxWidth(), padding = 17) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Column(Modifier.weight(1f)) {
                Label("주간 정상 자세 비율", 11, Muted)
                Label("67%", 32, Ink, FontWeight.Bold)
                Pill("▲ 지난주보다 6%p")
            }
            ProgressRing(67, Modifier.size(75.dp), stroke = 7f)
        }
    }
}

@Composable
private fun WeeklyChart() {
    RoundedCard(Modifier.fillMaxWidth(), padding = 17) {
        Row { Label("요일별 착석시간", 14, Ink, FontWeight.Bold, Modifier.weight(1f)); Label("단위: 시간", 10, Soft) }
        Spacer(Modifier.height(18.dp))
        val values = listOf(5.2f, 6.1f, 6.8f, 4.9f, 5.5f, 3.0f, 4.2f)
        val days = listOf("월", "화", "수", "목", "금", "토", "일")
        Row(Modifier.fillMaxWidth().height(180.dp), horizontalArrangement = Arrangement.SpaceEvenly, verticalAlignment = Alignment.Bottom) {
            values.forEachIndexed { index, value ->
                Column(horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.Bottom, modifier = Modifier.width(29.dp).fillMaxHeight()) {
                    Label("%.1f".format(value), 9, if (index == 6) Green else Soft)
                    Spacer(Modifier.height(5.dp))
                    val total = (value / 6.8f * 116).dp
                    Box(Modifier.width(23.dp).height(total * .35f).clip(RoundedCornerShape(5.dp)).background(Color(0xFFE1E4E6)))
                    Spacer(Modifier.height(2.dp))
                    Box(Modifier.width(23.dp).height(total * .65f).clip(RoundedCornerShape(5.dp)).background(if (index == 6) Green else Color(0xFF7CCB9B)))
                    Spacer(Modifier.height(6.dp))
                    Label(days[index], 10, if (index == 6) Green else Muted)
                }
            }
        }
        Spacer(Modifier.height(8.dp))
        Label("● 정상 자세    ▪ 그 외 자세", 10, Muted)
    }
}

@Composable
private fun WeeklyComparison() {
    RoundedCard(Modifier.fillMaxWidth(), padding = 16) {
        Row { Label("지난주와 비교", 14, Ink, FontWeight.Bold, Modifier.weight(1f)); Label("하루 평균", 10, Soft) }
        val rows = listOf(
            Triple("목 전방 자세", "58분 → 47분", "↓ 19%"), Triple("비대칭 착석", "38분 → 29분", "↓ 24%"),
            Triple("오른쪽 체중 편향", "20분 → 21분", "↑ 5%"), Triple("최대 연속 착석", "1시간 58분 → 1시간 46분", "↓ 10%"),
            Triple("화면과의 거리", "평균 49cm → 52cm", "↑ 6%")
        )
        rows.forEachIndexed { index, row ->
            Row(Modifier.fillMaxWidth().height(52.dp), verticalAlignment = Alignment.CenterVertically) {
                Box(Modifier.size(34.dp).clip(RoundedCornerShape(10.dp)).background(Background), contentAlignment = Alignment.Center) { Icon(listOf(R.drawable.user, R.drawable.legs, R.drawable.balance, R.drawable.clock, R.drawable.monitor)[index], Modifier.size(18.dp)) }
                Spacer(Modifier.width(10.dp))
                Column(Modifier.weight(1f)) { Label(row.first, 11, Ink, FontWeight.Bold); Label(row.second, 10, Muted) }
                Pill(row.third, green = index != 2)
            }
            if (index < rows.lastIndex) HorizontalDivider(color = Line)
        }
    }
}

@Composable
private fun DarkInsight(title: String, detail: String) {
    Row(Modifier.fillMaxWidth().clip(RoundedCornerShape(18.dp)).background(Ink).padding(16.dp), verticalAlignment = Alignment.CenterVertically) {
        Icon(R.drawable.info, Modifier.size(25.dp), Green)
        Spacer(Modifier.width(12.dp))
        Column { Label(title, 11, Soft); Label(detail, 12, Color.White, FontWeight.Bold) }
    }
}

@Composable
private fun FeedbackScreen(goals: Set<Int>, editGoals: () -> Unit) {
    LazyColumn(Modifier.fillMaxSize().background(Background), contentPadding = PaddingValues(start = 19.dp, end = 19.dp, top = 15.dp, bottom = 24.dp), verticalArrangement = Arrangement.spacedBy(15.dp)) {
        item { Label("피드백", 21, Ink, FontWeight.Bold) }
        item {
            Row(Modifier.fillMaxWidth().clip(RoundedCornerShape(22.dp)).background(Ink).padding(20.dp), verticalAlignment = Alignment.CenterVertically) {
                Column(Modifier.weight(1f)) {
                    Label("오늘 가장 많이 나타난 습관", 11, Soft)
                    Label("목 전방 자세", 19, Color.White, FontWeight.Bold)
                    Label("42분", 24, Green, FontWeight.Bold)
                    Label("목을 좌우로 15초씩 돌려주세요.", 11, Soft)
                }
                Box(Modifier.size(52.dp).clip(RoundedCornerShape(14.dp)).background(Color(0xFF292B2D)), contentAlignment = Alignment.Center) { Icon(R.drawable.user, Modifier.size(24.dp), Green) }
            }
        }
        item {
            Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                Label("내 목표", 15, Ink, FontWeight.Bold, Modifier.weight(1f))
                Label("편집", 12, Green, FontWeight.Bold, Modifier.clickable(onClick = editGoals))
            }
        }
        items(goals.sorted()) { index ->
            RoundedCard(Modifier.fillMaxWidth(), padding = 16) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Box(Modifier.size(40.dp).clip(RoundedCornerShape(12.dp)).background(PaleGreen), contentAlignment = Alignment.Center) { Icon(goalIcons[index], Modifier.size(20.dp), Green) }
                    Spacer(Modifier.width(12.dp))
                    Column(Modifier.weight(1f)) { Label(goalNames[index], 14, Ink, FontWeight.Bold); Label(if (index == 0) "비대칭 착석 · 다리 꼬기 추정" else goalDescriptions[index], 10, Muted) }
                    Pill("감소 추세")
                }
                Spacer(Modifier.height(11.dp))
                Row(Modifier.fillMaxWidth()) {
                    listOf("오늘" to if (index == 0) "25분" else "42분", "어제" to if (index == 0) "38분" else "51분", "지난주 평균" to if (index == 0) "38분" else "58분").forEach { (name, value) ->
                        Column(Modifier.weight(1f)) { Label(name, 10, Muted); Label(value, 15, if (name == "오늘") Green else Ink, FontWeight.Bold) }
                    }
                }
            }
        }
        item { Row { Label("오늘의 추천", 15, Ink, FontWeight.Bold, Modifier.weight(1f)); Label("누적 기록 기반", 11, Soft) } }
        item {
            RoundedCard(Modifier.fillMaxWidth(), padding = 16) {
                listOf(
                    Triple("목·어깨 가볍게 돌리기", "목 전방 자세가 42분 나타났어요", "1분"),
                    Triple("자리에서 일어나 걷기", "최대 연속 착석이 1시간 40분이었어요", "2분"),
                    Triple("좌우 균형 확인하기", "오른쪽으로 기댄 시간이 2.8배 길었어요", "10초"),
                    Triple("다리 풀고 양발 바닥에 두기", "비대칭 착석이 25분 있었어요", "수시로")
                ).forEachIndexed { index, item ->
                    Row(Modifier.fillMaxWidth().height(64.dp), verticalAlignment = Alignment.CenterVertically) {
                        Box(Modifier.size(38.dp).clip(RoundedCornerShape(10.dp)).background(Background), contentAlignment = Alignment.Center) { Icon(listOf(R.drawable.stretch, R.drawable.walk, R.drawable.balance, R.drawable.legs)[index], Modifier.size(20.dp)) }
                        Spacer(Modifier.width(10.dp))
                        Column(Modifier.weight(1f)) { Label(item.first, 12, Ink, FontWeight.Bold); Label(item.second, 10, Muted) }
                        Pill(item.third)
                    }
                    if (index < 3) HorizontalDivider(color = Line)
                }
            }
        }
        item { Label("ⓘ 생활습관 참고용 안내이며, 의료적 진단이나 치료를 대신하지 않아요.", 10, Soft) }
        item { Label("오늘의 자세 기록은 예시 데이터예요", 10, Soft, modifier = Modifier.fillMaxWidth(), align = TextAlign.Center) }
    }
}

@Composable
private fun SettingsScreen(
    connected: Boolean,
    goals: Set<Int>,
    alerts: Boolean,
    setAlerts: (Boolean) -> Unit,
    standAlert: Boolean,
    setStandAlert: (Boolean) -> Unit,
    threshold: Int,
    standMinutes: Int,
    onAction: (String) -> Unit
) {
    LazyColumn(Modifier.fillMaxSize().background(Background), contentPadding = PaddingValues(start = 16.dp, end = 16.dp, top = 13.dp, bottom = 20.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
        item { Label("설정", 21, Ink, FontWeight.Bold) }
        item {
            RoundedCard(Modifier.fillMaxWidth(), padding = 14) {
                Row { Label("기기 상태", 13, Ink, FontWeight.Bold, Modifier.weight(1f)); Pill(if (connected) "● 정상 작동" else "● 연결 대기") }
                SettingRow("카메라", if (connected) "정면 설치 · 연결됨" else "연결 대기", R.drawable.camera)
                SettingRow("좌면 압력센서", if (connected) "FSR 6개 · 10Hz" else "연결 대기", R.drawable.seat)
                SettingRow("거리 센서", if (connected) "HC-SR04 · 연결됨" else "연결 대기", R.drawable.distence)
                SettingRow("웹 연결", if (connected) "같은 Wi-Fi에서 접속 중" else "연결 대기", R.drawable.wifi)
                SettingRow("기기 재연결", "›", R.drawable.refresh, onClick = { onAction("기기 재연결") })
            }
        }
        item { Label("측정", 11, Muted, FontWeight.Bold) }
        item {
            RoundedCard(Modifier.fillMaxWidth(), padding = 13) {
                SettingRow("개선 목표", if (goals.isEmpty()) "선택 안 함" else "${goalNames[goals.first()]} 외 ${goals.size - 1}개", R.drawable.target, onClick = { onAction("개선 목표") })
                SettingRow("기준 자세 다시 측정", "›", R.drawable.refresh, onClick = { onAction("기준 자세 다시 측정") })
                SettingRow("자세 인정 기준", "3초 이상 유지 ›", R.drawable.clock, onClick = { onAction("자세 인정 기준") })
            }
        }
        item { Label("알림", 11, Muted, FontWeight.Bold) }
        item {
            RoundedCard(Modifier.fillMaxWidth(), padding = 13) {
                SettingRow("실시간 자세 알림", "", R.drawable.bell, toggle = alerts, onToggle = setAlerts)
                SettingRow("알림 기준", "${threshold}분 이상 지속 시 ›", R.drawable.clock, onClick = { onAction("알림 기준") })
                SettingRow("일어나기 알림", "${standMinutes}분 연속 착석 시", R.drawable.walk, toggle = standAlert, onToggle = setStandAlert, onClick = { onAction("일어나기 알림") })
            }
        }
        item { Label("데이터 · 프라이버시", 11, Muted, FontWeight.Bold) }
        item {
            RoundedCard(Modifier.fillMaxWidth(), padding = 13) {
                SettingRow("카메라 영상", "저장 안 함", R.drawable.shield)
                SettingRow("기록 내보내기", "CSV ›", R.drawable.download, onClick = { onAction("기록 내보내기") })
                SettingRow("기록 초기화", "›", R.drawable.trash, color = Danger, onClick = { onAction("기록 초기화") })
            }
        }
        item { Label("바른자리 v0.1 · 연구용 데모", 10, Soft, modifier = Modifier.fillMaxWidth(), align = TextAlign.Center) }
    }
}

@Composable
private fun SettingRow(
    name: String,
    value: String,
    icon: Int,
    color: Color = Ink,
    onClick: (() -> Unit)? = null,
    toggle: Boolean? = null,
    onToggle: ((Boolean) -> Unit)? = null
) {
    Row(Modifier.fillMaxWidth().height(45.dp).then(if (onClick != null) Modifier.clickable(onClick = onClick) else Modifier), verticalAlignment = Alignment.CenterVertically) {
        Box(Modifier.size(29.dp).clip(RoundedCornerShape(8.dp)).background(Background), contentAlignment = Alignment.Center) { Icon(icon, Modifier.size(17.dp), if (color == Danger) Danger else Slate) }
        Spacer(Modifier.width(10.dp))
        Label(name, 11, color, FontWeight.SemiBold, Modifier.weight(1f))
        Label(value, 10, if (value == "저장 안 함") Green else Muted)
        if (toggle != null) {
            Spacer(Modifier.width(6.dp))
            Switch(toggle, onCheckedChange = onToggle, modifier = Modifier.height(29.dp), colors = SwitchDefaults.colors(checkedTrackColor = Green))
        }
    }
    HorizontalDivider(color = Line)
}

@Composable
private fun DeviceScreen(connected: Boolean, toggleConnection: () -> Unit) {
    LazyColumn(Modifier.fillMaxSize().background(Background), contentPadding = PaddingValues(start = 20.dp, end = 20.dp, top = 17.dp, bottom = 24.dp), verticalArrangement = Arrangement.spacedBy(19.dp)) {
        item { Label("기기 재연결", 23, Ink, FontWeight.Bold) }
        item {
            Row(Modifier.fillMaxWidth().clip(RoundedCornerShape(22.dp)).background(if (connected) Color.White else Ink).padding(20.dp), verticalAlignment = Alignment.CenterVertically) {
                Column(Modifier.weight(1f)) { Label("기기 연결 상태", 12, if (connected) Ink else Soft); Label(if (connected) "모두 연결되어 있어요." else "연결되어 있지 않아요.", 20, if (connected) Ink else Color.White, FontWeight.Bold) }
                Box(Modifier.size(56.dp).clip(RoundedCornerShape(14.dp)).background(if (connected) Mint else Color(0xFF777B80)), contentAlignment = Alignment.Center) { Icon(R.drawable.user, Modifier.size(25.dp), if (connected) Green else Slate) }
            }
        }
        item { Label("기기 연결 상태", 15, Ink, FontWeight.Bold) }
        item {
            RoundedCard(Modifier.fillMaxWidth(), padding = 15) {
                listOf(Triple("카메라", "상체 인식됨", R.drawable.camera), Triple("좌면 압력센서", "6개 인식됨", R.drawable.seat), Triple("거리 센서", "거리 인식됨", R.drawable.distence)).forEachIndexed { index, row ->
                    Row(Modifier.fillMaxWidth().height(39.dp), verticalAlignment = Alignment.CenterVertically) {
                        Icon(row.third, Modifier.size(20.dp))
                        Spacer(Modifier.width(13.dp))
                        Label(row.first, 12, Ink, FontWeight.SemiBold, Modifier.weight(1f))
                        Label(if (connected) row.second else "연결 대기", 11, if (connected) Green else Muted)
                        if (connected) { Spacer(Modifier.width(6.dp)); Icon(R.drawable.check, Modifier.size(14.dp), Green) }
                    }
                    if (index < 2) HorizontalDivider(color = Line)
                }
            }
        }
        item { Label("기기 목록", 15, Ink, FontWeight.Bold) }
        item {
            RoundedCard(Modifier.fillMaxWidth(), padding = 18) {
                Label("바른자리", 15, Ink, FontWeight.Bold)
                Label("시연용 기기", 12, Muted)
                Spacer(Modifier.height(11.dp))
                PrimaryButton(if (connected) "기기 연결 끊기" else "이 기기 연결하기", toggleConnection, modifier = Modifier.height(42.dp), color = if (connected) Danger else Green)
            }
        }
        item { Label("ⓘ 현재 버튼은 연결 상태를 시연해요. 실제 기기 통신은 아직 연결되지 않았어요.", 11, Soft) }
    }
}
