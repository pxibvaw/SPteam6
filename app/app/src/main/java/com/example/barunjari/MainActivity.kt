package com.example.barunjari

import android.os.Bundle
import android.os.SystemClock
import androidx.activity.compose.BackHandler
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
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
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.SpanStyle
import androidx.compose.ui.text.buildAnnotatedString
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.withStyle
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.core.view.WindowCompat
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
private val PlayerInk = Color(0xFF0F3D22)
private const val OnboardingDesignVersion = 3

private enum class Page { ONBOARDING, GOALS, CONNECTION, CALIBRATION, HOME, REPORT, FEEDBACK, SETTINGS, DEVICE, MEASUREMENT }
private val goalNames = listOf("다리 꼬기 줄이기", "목 앞으로 내미는 자세 줄이기", "한쪽으로 기대는 습관 줄이기", "장시간 연속 착석 줄이기")
private val goalDescriptions = listOf("비대칭 착석(다리 꼬기 추정) 시간을 추적해요", "목 전방 자세가 이어진 시간을 추적해요", "좌우 체중 편향 시간을 비교해요", "최대 연속 착석시간을 알려드려요")
private val goalIcons = listOf(R.drawable.legs, R.drawable.user, R.drawable.balance, R.drawable.clock)

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        WindowCompat.getInsetsController(window, window.decorView).apply {
            isAppearanceLightStatusBars = true
            isAppearanceLightNavigationBars = true
        }
        val prefs = getSharedPreferences("sitsense", MODE_PRIVATE)
        setContent {
            key(OnboardingDesignVersion) {
                SitSenseApp(
                    savedGoals = prefs.getString("goals", "0,1") ?: "0,1",
                    saveOnboarding = { prefs.edit().putBoolean("first_run_done", true)
                        .putInt("onboarding_design_version", OnboardingDesignVersion).apply() },
                    saveGoals = { selected -> prefs.edit().putString("goals", selected.sorted().joinToString(",")).apply() },
                    resetAll = { prefs.edit().clear().apply() }
                )
            }
        }
    }
}

@Composable
private fun SitSenseApp(
    savedGoals: String,
    saveOnboarding: () -> Unit,
    saveGoals: (Set<Int>) -> Unit,
    resetAll: () -> Unit
) {
    // Saved completion must not skip setup on a fresh launch; recreation retains this session.
    var page by rememberSaveable { mutableStateOf(Page.GOALS) }
    var onboardingComplete by rememberSaveable { mutableStateOf(false) }
    var goals by rememberSaveable { mutableStateOf(savedGoals.split(",").mapNotNull(String::toIntOrNull)
        .filter { it in goalNames.indices }.distinct().take(2).toSet()) }
    var editingGoals by rememberSaveable { mutableStateOf(false) }
    var connected by rememberSaveable { mutableStateOf(false) }
    var alerts by rememberSaveable { mutableStateOf(true) }
    var standAlert by rememberSaveable { mutableStateOf(true) }
    var threshold by rememberSaveable { mutableIntStateOf(5) }
    var standMinutes by rememberSaveable { mutableIntStateOf(50) }
    var weekly by rememberSaveable { mutableStateOf(false) }
    var selectedDate by rememberSaveable { mutableLongStateOf(ReportDates.today()) }
    var measurementStatus by rememberSaveable { mutableStateOf(MeasurementStatus.IDLE) }
    var measurementMillis by rememberSaveable { mutableLongStateOf(0L) }
    var measurementBaseMillis by rememberSaveable { mutableLongStateOf(0L) }
    var measurementStartedAt by rememberSaveable { mutableLongStateOf(0L) }
    var showAlertCard by rememberSaveable { mutableStateOf(true) }
    var dialog by rememberSaveable { mutableStateOf("") }

    fun stopMeasurement() {
        measurementStatus = MeasurementStatus.IDLE
        measurementMillis = 0L
        measurementBaseMillis = 0L
        if (page == Page.MEASUREMENT) page = Page.HOME
    }
    fun toggleMeasurement() {
        if (!connected) {
            page = Page.DEVICE
        } else if (measurementStatus == MeasurementStatus.RUNNING) {
            measurementMillis = measurementElapsed(measurementBaseMillis, measurementStartedAt, SystemClock.elapsedRealtime())
            measurementBaseMillis = measurementMillis
            measurementStatus = MeasurementStatus.PAUSED
        } else {
            measurementStartedAt = SystemClock.elapsedRealtime()
            measurementStatus = MeasurementStatus.RUNNING
            page = Page.MEASUREMENT
        }
    }
    LaunchedEffect(measurementStatus) {
        while (measurementStatus == MeasurementStatus.RUNNING) {
            measurementMillis = measurementElapsed(measurementBaseMillis, measurementStartedAt, SystemClock.elapsedRealtime())
            delay(200)
        }
    }
    BackHandler(enabled = page == Page.MEASUREMENT) { page = Page.HOME }

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
                        page = if (editingGoals) Page.SETTINGS else Page.CONNECTION
                        editingGoals = false
                    }
                )
                Page.CONNECTION -> ConnectionOnboardingScreen(
                    includeConnection = true,
                    onBack = { connected = false; page = if (onboardingComplete) Page.DEVICE else Page.GOALS },
                    onDone = { connected = true; page = Page.CALIBRATION }
                )
                Page.CALIBRATION -> ConnectionOnboardingScreen(
                    includeConnection = false,
                    onBack = { page = if (onboardingComplete) Page.SETTINGS else Page.CONNECTION },
                    onDone = { saveOnboarding(); onboardingComplete = true; page = Page.HOME }
                )
                else -> {
                    Box(Modifier.weight(1f)) {
                        when (page) {
                            Page.HOME -> HomeScreen(goals, showAlertCard, { showAlertCard = false }, { page = Page.FEEDBACK })
                            Page.REPORT -> ReportScreen(weekly, { weekly = it }, selectedDate, { selectedDate = it })
                            Page.MEASUREMENT -> MeasurementScreen(measurementStatus, measurementMillis, { page = Page.HOME })
                            Page.FEEDBACK -> FeedbackScreen(goals, { editingGoals = true; page = Page.GOALS })
                            Page.SETTINGS -> SettingsScreen(
                                connected, goals, alerts, { alerts = it }, standAlert, { standAlert = it },
                                threshold, standMinutes,
                                onAction = { action ->
                                    when (action) {
                                        "기기 재연결" -> page = Page.DEVICE
                                        "개선 목표" -> { editingGoals = true; page = Page.GOALS }
                                        "기준 자세 다시 측정" -> {
                                            stopMeasurement()
                                            page = if (connected) Page.CALIBRATION else Page.CONNECTION
                                        }
                                        else -> dialog = action
                                    }
                                }
                            )
                            Page.DEVICE -> DeviceScreen(connected, {
                                stopMeasurement()
                                if (connected) connected = false else page = Page.CONNECTION
                            })
                            else -> Unit
                        }
                    }
                    if (page in listOf(Page.HOME, Page.REPORT, Page.FEEDBACK, Page.SETTINGS, Page.DEVICE, Page.MEASUREMENT)) {
                        MeasurementPlayer(measurementStatus, measurementMillis, ::toggleMeasurement, { dialog = "측정 종료" },
                            onOpen = { if (measurementStatus == MeasurementStatus.IDLE) toggleMeasurement() else page = Page.MEASUREMENT })
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
                        "측정 종료" -> Text("오늘 측정을 마칠까요?")
                        else -> Text("연결된 센서가 준비되면 사용할 수 있어요.")
                    }
                },
                confirmButton = { TextButton(onClick = {
                    if (dialog == "측정 종료") stopMeasurement()
                    if (dialog == "기록 초기화") {
                        resetAll()
                        goals = setOf(0, 1)
                        onboardingComplete = false
                        connected = false
                        stopMeasurement()
                        page = Page.GOALS
                    }
                    dialog = ""
                }) { Text(when (dialog) { "기록 초기화" -> "초기화"; "측정 종료" -> "종료"; else -> "확인" }) } },
                dismissButton = {
                    if (dialog in listOf("알림 기준", "일어나기 알림", "기록 초기화", "측정 종료")) {
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
            val active = when (page) { Page.DEVICE -> target == Page.SETTINGS; Page.MEASUREMENT -> target == Page.HOME; else -> target == page }
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
            IconImage(R.drawable.design_onboarding, Modifier.fillMaxWidth().height(330.dp))
            Spacer(Modifier.height(26.dp))
            Text(buildAnnotatedString {
                append("앉아만 있어도\n자세 습관이 ")
                withStyle(SpanStyle(color = Green)) { append("기록돼요") }
            }, fontSize = 28.sp, fontWeight = FontWeight.Bold, lineHeight = 38.sp, color = Ink)
            Spacer(Modifier.height(14.dp))
            Text("카메라·적외선 거리센서와 의자 압력센서로\n상체와 하체 자세를 함께 분석하고,\n하루·한 주의 자세 습관을 리포트로 보여드려요.", color = Muted, fontSize = 15.sp, lineHeight = 23.25.sp, letterSpacing = (-.3).sp)
            Spacer(Modifier.height(20.dp))
            Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                listOf("상체 분석" to R.drawable.camera, "착석·체중 분포" to R.drawable.seat, "화면 거리 측정" to R.drawable.distence).forEach { (title, icon) ->
                    Row(Modifier.clip(CircleShape).background(Color(0xFFF3F4F6)).padding(horizontal = 10.dp, vertical = 7.dp), verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                        Icon(icon, Modifier.size(15.dp), Green)
                        Label(title, 11, Slate)
                    }
                }
            }
            Spacer(Modifier.weight(1f))
            PrimaryButton("시작하기", onStart)
            Spacer(Modifier.height(22.dp))
            Label("평소 쓰는 의자에서 착용 장비 없이 · 영상은 저장하지 않아요", 12, Soft, modifier = Modifier.fillMaxWidth(), align = TextAlign.Center)
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
private fun ProgressRing(percent: Int, modifier: Modifier = Modifier, color: Color = Green, base: Color = PaleGreen, stroke: Float = 8f) =
    ProgressRing(percent.toFloat(), modifier, color, base, stroke)

@Composable
private fun ProgressRing(percent: Float, modifier: Modifier = Modifier, color: Color = Green, base: Color = PaleGreen, stroke: Float = 8f) {
    Canvas(modifier) {
        drawArc(base, -90f, 360f, false, style = Stroke(stroke.dp.toPx(), cap = StrokeCap.Butt))
        drawArc(color, -90f, 360f * percent / 100f, false, style = Stroke(stroke.dp.toPx(), cap = StrokeCap.Butt))
    }
}

@Composable
private fun StepHeader(step: Int, onBack: () -> Unit, total: Int = 2) {
    Row(Modifier.fillMaxWidth().height(30.dp), verticalAlignment = Alignment.CenterVertically) {
        Box(Modifier.size(28.dp).clickable(onClick = onBack), contentAlignment = Alignment.CenterStart) {
            Icon(R.drawable.chevl, Modifier.size(24.dp), Ink)
        }
        Spacer(Modifier.weight(1f))
        Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
            repeat(total) { index -> Box(Modifier.size(28.dp, 4.dp).clip(CircleShape).background(if (index < step) Green else Line)) }
        }
        Spacer(Modifier.weight(1f))
        Label("$step / $total", 12, Muted)
    }
}

@Composable
private fun GoalsScreen(selected: Set<Int>, editing: Boolean, onBack: () -> Unit, onToggle: (Int) -> Unit, onNext: () -> Unit) {
    Column(Modifier.fillMaxSize().background(Color.White).padding(horizontal = 20.dp).padding(top = 8.dp, bottom = 26.dp)) {
        if (editing) Row(Modifier.height(34.dp).clickable(onClick = onBack), verticalAlignment = Alignment.CenterVertically) { Icon(R.drawable.chevl, Modifier.size(24.dp), Ink); Spacer(Modifier.width(8.dp)); Label("목표 수정", 18, Ink, FontWeight.Bold) }
        else StepHeader(1, onBack, total = 3)
        Spacer(Modifier.height(22.dp))
        Text("어떤 습관을\n가장 줄이고 싶나요?", fontSize = 26.sp, lineHeight = 35.1.sp, letterSpacing = (-.52).sp, color = Ink, fontWeight = FontWeight.Bold)
        Spacer(Modifier.height(10.dp))
        Label("선택한 목표와 관련된 지표를 리포트에서 먼저 보여드려요. 최대 두 개 선택할 수 있어요.", 14, Muted)
        Spacer(Modifier.height(24.dp))
        goalNames.forEachIndexed { index, name ->
            val chosen = index in selected
            Row(Modifier.fillMaxWidth().padding(bottom = 12.dp).height(79.dp)
                .clip(RoundedCornerShape(18.dp)).background(if (chosen) Color(0xFFF4FBF6) else Color.White)
                .border(if (chosen) 1.5.dp else 1.dp, if (chosen) Green else Line, RoundedCornerShape(18.dp))
                .clickable { onToggle(index) }.padding(horizontal = 16.dp), verticalAlignment = Alignment.CenterVertically) {
                Box(Modifier.size(44.dp).clip(RoundedCornerShape(13.dp)).background(if (chosen) Green else Color(0xFFF3F4F6)), contentAlignment = Alignment.Center) {
                    Icon(goalIcons[index], Modifier.size(22.dp), if (chosen) Color.White else Slate)
                }
                Spacer(Modifier.width(14.dp))
                Column(Modifier.weight(1f)) {
                    Label(name, 15, Ink, FontWeight.Bold)
                    Label(listOf("다리 꼬기 시간과 방향을 추적해요", "거북목 자세가 이어진 시간을 추적해요", "좌우 체중 편향 시간을 비교해요", "최대 연속 착석시간을 알려드려요")[index], 12, Muted)
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
private fun HomeScreen(goals: Set<Int>, alertVisible: Boolean, dismissAlert: () -> Unit, openFeedback: () -> Unit) {
    LazyColumn(Modifier.fillMaxSize().background(Background), contentPadding = PaddingValues(start = 20.dp, end = 20.dp, top = 8.dp, bottom = 24.dp), verticalArrangement = Arrangement.spacedBy(14.dp)) {
        item {
            Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                Column(Modifier.weight(1f)) {
                    Label(ReportDates.format(ReportDates.today(), "M월 d일 EEEE"), 13, Muted)
                    Label("좋은 오후예요, 송이님", 22, Ink, FontWeight.Bold)
                }
                Box(Modifier.size(42.dp).clip(CircleShape).background(Color.White).clickable(onClick = openFeedback), contentAlignment = Alignment.Center) {
                    Icon(R.drawable.bell, Modifier.size(20.dp), Ink)
                }
            }
        }
        item {
            DesignCard(Modifier.fillMaxWidth(), padding = 20) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                        Pill("● 하루 평균 자세")
                        Label("목이 앞으로 기울었어요.", 12)
                        Label("거북목 자세", 25, Ink, FontWeight.Bold)
                        Label("편향 비율 · 계산식 확인 중", 12)
                    }
                    IconImage(R.drawable.design_summary_plant, Modifier.size(101.dp, 97.dp))
                }
            }
        }
        item {
            Row(Modifier.fillMaxWidth().clip(RoundedCornerShape(24.dp)).background(Green).padding(20.dp), verticalAlignment = Alignment.CenterVertically) {
                Column(Modifier.weight(1f)) {
                    Label("오늘 정상 자세 비율", 14, Color.White.copy(alpha = .85f))
                    Label("${DemoPostureReport.normalPercent}%", 42, Color.White, FontWeight.Bold)
                    Label("총 착석 4시간 12분 · 정상 3시간 1분", 12, Color.White)
                }
                ProgressRing(DemoPostureReport.normalPercent, Modifier.size(88.dp), Color.White, Color.White.copy(alpha = .25f), 9f)
            }
        }
        if (alertVisible) item {
            Row(Modifier.fillMaxWidth().clip(RoundedCornerShape(18.dp)).background(Ink).padding(14.dp), verticalAlignment = Alignment.CenterVertically) {
                Box(Modifier.size(38.dp).clip(RoundedCornerShape(12.dp)).background(Color.White.copy(alpha = .1f)), contentAlignment = Alignment.Center) {
                    Icon(R.drawable.alert, Modifier.size(19.dp), Green)
                }
                Spacer(Modifier.width(10.dp))
                Column(Modifier.weight(1f)) {
                    Label("거북목 자세가 5분째 이어지고 있어요", 13, Color.White, FontWeight.Bold)
                    Label("목과 어깨를 가볍게 움직여 보세요", 12, Soft)
                }
                Box(Modifier.size(24.dp).clickable(onClick = dismissAlert), contentAlignment = Alignment.Center) { Icon(R.drawable.x, Modifier.size(17.dp), Soft) }
            }
        }
        item {
            Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                Label("지금 자세", 17, Ink, FontWeight.Bold, Modifier.weight(1f))
                Label("상체·하체를 따로 기록해요", 10, Soft)
            }
        }
        item {
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                DesignCard(Modifier.weight(1f).height(252.dp), padding = 16) {
                    Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                        Icon(R.drawable.camera, Modifier.size(15.dp), Muted)
                        Label("적외선 거리 · 카메라", 10, Muted)
                    }
                    Spacer(Modifier.height(8.dp))
                    Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                        Box(Modifier.size(8.dp).clip(CircleShape).background(Green))
                        Label("상체 정상", 18, Ink, FontWeight.Bold)
                    }
                    Spacer(Modifier.height(8.dp))
                    PostureGauges()
                    Spacer(Modifier.height(10.dp))
                    Label("처음 측정한 기준 자세와 비교해요", 10, Soft)
                }
                DesignCard(Modifier.weight(1f).height(252.dp), padding = 16) {
                    Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                        Icon(R.drawable.seat, Modifier.size(15.dp), Muted)
                        Label("좌면 · 압력센서", 10, Muted)
                    }
                    Spacer(Modifier.height(8.dp))
                    Label("● 오른쪽 편향", 16, Ink, FontWeight.Bold)
                    Spacer(Modifier.height(8.dp))
                    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(4.dp)) {
                        listOf(7, 8).forEach { PressureCell(it, Modifier.weight(1f)) }
                    }
                    Spacer(Modifier.height(4.dp))
                    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(4.dp)) {
                        listOf(14, 17).forEach { PressureCell(it, Modifier.weight(1f)) }
                    }
                    Spacer(Modifier.height(4.dp))
                    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(4.dp)) {
                        listOf(23, 31).forEach { PressureCell(it, Modifier.weight(1f)) }
                    }
                    Spacer(Modifier.height(8.dp))
                    Label("앞·뒤 / 좌·우 압력 비율\nFSR 6개 기준", 10, Soft)
                }
            }
        }
        item {
            DesignCard(Modifier.fillMaxWidth(), padding = 16) {
                Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                    Label("내 목표", 16, Ink, FontWeight.Bold, Modifier.weight(1f))
                    Label("피드백 보기 ›", 11, Green, FontWeight.Bold, Modifier.clickable(onClick = openFeedback))
                }
                Spacer(Modifier.height(7.dp))
                goals.sorted().forEachIndexed { order, index ->
                    Row(Modifier.fillMaxWidth().padding(vertical = 8.dp), verticalAlignment = Alignment.CenterVertically) {
                        Box(Modifier.size(38.dp).clip(RoundedCornerShape(11.dp)).background(Background), contentAlignment = Alignment.Center) { Icon(goalIcons[index], Modifier.size(18.dp)) }
                        Spacer(Modifier.width(10.dp))
                        Column(Modifier.weight(1f)) {
                            Label(if (index == 1) "거북목 자세 줄이기" else goalNames[index], 14, Ink, FontWeight.Bold)
                            Label(if (index == 0) "오늘 25분 · 어제 38분" else if (index == 1) "오늘 42분 · 어제 51분" else "오늘 기록 중", 12, Muted)
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
    val shade = when { value >= 30 -> Green; value >= 20 -> Color(0xFF77CAA0); value >= 14 -> Color(0xFF93D4AB); else -> Color(0xFFB6E3C5) }
    Box(modifier.height(35.dp).clip(RoundedCornerShape(8.dp)).background(shade), contentAlignment = Alignment.Center) {
        Label("$value%", 12, if (value >= 20) Color.White else Color(0xFF087B36), FontWeight.Bold)
    }
}

@Composable
@OptIn(ExperimentalMaterial3Api::class)
private fun ReportScreen(weekly: Boolean, setWeekly: (Boolean) -> Unit, selectedDate: Long, selectDate: (Long) -> Unit) {
    var calendarOpen by rememberSaveable { mutableStateOf(false) }
    val today = ReportDates.today()
    if (calendarOpen) {
        ReportCalendar(selectedDate, weekly, { calendarOpen = false }, selectDate)
    }
    LazyColumn(Modifier.fillMaxSize().background(Background), contentPadding = PaddingValues(start = 20.dp, end = 20.dp, top = 8.dp, bottom = 24.dp), verticalArrangement = Arrangement.spacedBy(14.dp)) {
        item {
            Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                Label("리포트", 24, Ink, FontWeight.Bold, Modifier.weight(1f))
                Box(Modifier.size(42.dp).clip(CircleShape).background(Color.White).semantics { contentDescription = "리포트 날짜 선택" }.clickable { calendarOpen = true }, contentAlignment = Alignment.Center) { Icon(R.drawable.calendar, Modifier.size(20.dp), Ink) }
            }
        }
        item {
            Row(Modifier.fillMaxWidth().height(46.dp).clip(RoundedCornerShape(13.dp)).background(Color(0xFFE9EBEA)).padding(4.dp)) {
                listOf(false to "일간", true to "주간").forEach { (mode, title) ->
                    Box(Modifier.weight(1f).fillMaxHeight().clip(RoundedCornerShape(10.dp)).background(if (weekly == mode) Color.White else Color.Transparent).clickable { setWeekly(mode) }, contentAlignment = Alignment.Center) {
                        Label(title, 14, if (weekly == mode) Ink else Muted, if (weekly == mode) FontWeight.Bold else FontWeight.Normal)
                    }
                }
            }
        }
        item {
            Row(Modifier.fillMaxWidth().height(34.dp), verticalAlignment = Alignment.CenterVertically) {
                Box(Modifier.size(34.dp).clip(CircleShape).background(Color.White).semantics { contentDescription = "이전 기간" }.clickable { selectDate(ReportDates.shift(selectedDate, if (weekly) -7 else -1)) }, contentAlignment = Alignment.Center) { Icon(R.drawable.chevl, Modifier.size(18.dp)) }
                Label(ReportDates.label(selectedDate, weekly), 13, Ink, FontWeight.Bold, Modifier.weight(1f).clickable { calendarOpen = true }, TextAlign.Center)
                val nextDate = ReportDates.shift(selectedDate, if (weekly) 7 else 1)
                Box(Modifier.size(34.dp).clip(CircleShape).background(Color.White).semantics { contentDescription = "다음 기간" }.clickable(enabled = nextDate <= today) { selectDate(nextDate) }, contentAlignment = Alignment.Center) { Icon(R.drawable.chevr, Modifier.size(18.dp), if (nextDate <= today) Slate else Soft) }
            }
        }
        item { Label("선택한 기간의 기록은 아직 연결되지 않았어요. 아래 통계는 화면 예시예요.", 10, Muted) }
        if (weekly) {
            item { WeeklySummary() }
            item { WeeklyChart(selectedDate) }
            item { WeeklyComparison() }
            item { DarkInsight("이번 주 패턴", "오후 3시 이후에 자세가 가장 자주 무너졌어요") }
        } else {
            item {
                Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        MetricCard("총 착석시간", "4시간 12분", Modifier.weight(1f))
                        MetricCard("정상 자세", "${DemoPostureReport.normalPercent}%", Modifier.weight(1f), Green)
                    }
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        MetricCard("최대 연속 착석", "1시간 40분", Modifier.weight(1f))
                        MetricCard("자세 무너짐 시작", "평균 29분 후", Modifier.weight(1f))
                    }
                }
            }
            item {
                DesignCard(Modifier.fillMaxWidth(), padding = 15) {
                    Row { Label("자세별 누적시간", 14, Ink, FontWeight.Bold, Modifier.weight(1f)); Label("착석 4시간 12분 기준", 10, Soft) }
                    Spacer(Modifier.height(12.dp))
                    // Exclusive demo totals: 181 + 42 + 17 + 8 + 4 = 252 minutes.
                    listOf(Triple("정상 자세", "3시간 1분", 181f / 252), Triple("거북목 자세", "42분", 42f / 252), Triple("다리 꼬기 · 오른다리 12분 / 왼다리 5분", "17분", 17f / 252), Triple("오른쪽 체중 편향", "8분", 8f / 252), Triple("왼쪽 체중 편향", "4분", 4f / 252)).forEachIndexed { i, row ->
                        Row { Label(row.first, 12, Slate, modifier = Modifier.weight(1f)); Label(row.second, 12, Ink, FontWeight.Bold) }
                        Spacer(Modifier.height(5.dp))
                        RatioBar(row.third, if (i == 0) Green else if (i == 1) Ink else Soft)
                        Spacer(Modifier.height(9.dp))
                    }
                }
            }
            item {
                DesignCard(Modifier.fillMaxWidth(), padding = 16) {
                    Label("좌우 체중 편향 비교", 14, Ink, FontWeight.Bold)
                    Spacer(Modifier.height(11.dp))
                    Row { Column(Modifier.weight(1f)) { Label("왼쪽", 10, Muted); Label("4분", 17, Slate, FontWeight.Bold) }; Column(horizontalAlignment = Alignment.End) { Label("오른쪽", 10, Muted); Label("8분", 17, Ink, FontWeight.Bold) } }
                    Spacer(Modifier.height(9.dp))
                    Row(Modifier.fillMaxWidth().height(10.dp), horizontalArrangement = Arrangement.spacedBy(3.dp)) {
                        Box(Modifier.weight(1f).fillMaxHeight().clip(RoundedCornerShape(5.dp)).background(Color(0xFFCBD0D4)))
                        Box(Modifier.weight(2f).fillMaxHeight().clip(RoundedCornerShape(5.dp)).background(Ink))
                    }
                    Spacer(Modifier.height(10.dp))
                    Label("ⓘ 오른쪽 체중 편향 시간이 왼쪽보다 2배 길어요", 11, Slate)
                }
            }
            item {
                DesignCard(Modifier.fillMaxWidth(), padding = 16) {
                    Label("화면과의 거리", 14, Ink, FontWeight.Bold)
                    Spacer(Modifier.height(9.dp))
                    Row { Column(Modifier.weight(1f)) { Label("평균 거리", 10, Muted); Label("52cm", 18, Slate, FontWeight.Bold) }; Column(horizontalAlignment = Alignment.End) { Label("가까웠던 시간", 10, Muted); Label("38분", 18, Ink, FontWeight.Bold) } }
                    Spacer(Modifier.height(9.dp))
                    Row(Modifier.fillMaxWidth().height(12.dp).clip(CircleShape), horizontalArrangement = Arrangement.spacedBy(4.dp)) {
                        Box(Modifier.weight(.84f).fillMaxHeight().background(Color(0xFFCBD0D4)))
                        Box(Modifier.weight(.16f).fillMaxHeight().background(Ink))
                    }
                    Spacer(Modifier.height(9.dp))
                    Label("ⓘ 기준보다 10cm 이상 가까웠던 시간이 38분이에요", 11, Slate)
                }
            }
            item {
                DesignCard(Modifier.fillMaxWidth(), padding = 17) {
                    Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                        Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                            Label("오늘 가장 많이 나타난 습관", 12, Soft)
                            Label(DemoPostureReport.habit.label, 22, Ink, FontWeight.Bold)
                            Label("${DemoPostureReport.exclusiveMinutes.getValue(DemoPostureReport.habit)}분", 17, Ink, FontWeight.Bold)
                        }
                        Box(Modifier.size(56.dp).clip(RoundedCornerShape(16.dp)).background(Color(0xFFF3F4F6)), contentAlignment = Alignment.Center) {
                            IconImage(R.drawable.design_neck_habit, Modifier.size(42.dp, 35.dp))
                        }
                    }
                }
            }
        }
        item { Label("현재 리포트 수치는 예시 데이터예요", 10, Soft, modifier = Modifier.fillMaxWidth(), align = TextAlign.Center) }
    }
}

@Composable
private fun MetricCard(title: String, value: String, modifier: Modifier = Modifier, valueColor: Color = Ink) {
    DesignCard(modifier.height(70.dp), padding = 14) {
        Label(title, 10, Muted)
        Label(value, 17, valueColor, FontWeight.Bold)
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
    DesignCard(Modifier.fillMaxWidth(), padding = 17) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Column(Modifier.weight(1f)) {
                Label("주간 정상 자세 비율", 13, Muted)
                Label("${DemoPostureReport.weeklyPercent}%", 36, Ink, FontWeight.Bold)
                Pill("착석시간 합계 기준 · 예시")
            }
            ProgressRing(DemoPostureReport.weeklyPercent, Modifier.size(84.dp), stroke = 8f)
        }
    }
}

@Composable
private fun WeeklyChart(selectedDate: Long) {
    DesignCard(Modifier.fillMaxWidth(), padding = 17) {
        Row { Label("요일별 착석시간", 14, Ink, FontWeight.Bold, Modifier.weight(1f)); Label("단위: 시간", 10, Soft) }
        Spacer(Modifier.height(18.dp))
        val values = DemoPostureReport.weeklySeatedMinutes.map { it / 60f }
        val days = ReportDates.calendarWeek(selectedDate).map { ReportDates.format(it, "E") }
        Row(Modifier.fillMaxWidth().height(180.dp), horizontalArrangement = Arrangement.SpaceEvenly, verticalAlignment = Alignment.Bottom) {
            values.forEachIndexed { index, value ->
                Column(horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.Bottom, modifier = Modifier.width(29.dp).fillMaxHeight()) {
                    Label("%.1f".format(value), 9, if (index == 6) Green else Soft)
                    Spacer(Modifier.height(5.dp))
                    val total = (value / 6.8f * 116).dp
                    val normalRatio = DemoPostureReport.weeklyNormalMinutes[index].toFloat() / DemoPostureReport.weeklySeatedMinutes[index]
                    Box(Modifier.width(23.dp).height(total * (1f - normalRatio)).clip(RoundedCornerShape(5.dp)).background(Color(0xFFE1E4E6)))
                    Spacer(Modifier.height(2.dp))
                    Box(Modifier.width(23.dp).height(total * normalRatio).clip(RoundedCornerShape(5.dp)).background(if (index == 6) Green else Color(0xFF7CCB9B)))
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
    DesignCard(Modifier.fillMaxWidth(), padding = 16) {
        Row { Label("지난주와 비교", 14, Ink, FontWeight.Bold, Modifier.weight(1f)); Label("하루 평균", 10, Soft) }
        val rows = listOf(
            Triple("거북목 자세", "58분 → 47분", "↓ 19%"), Triple("다리 꼬기", "38분 → 29분", "↓ 24%"),
            Triple("몸 기울기 · 카메라", "오른쪽 20분 → 21분", "↑ 5%"), Triple("최대 연속 착석", "1시간 58분 → 1시간 46분", "↓ 10%"),
            Triple("화면과의 거리", "평균 49cm → 52cm", "↑ 6%")
        )
        rows.forEachIndexed { index, row ->
            Row(Modifier.fillMaxWidth().height(63.dp), verticalAlignment = Alignment.CenterVertically) {
                Box(Modifier.size(38.dp).clip(RoundedCornerShape(11.dp)).background(Background), contentAlignment = Alignment.Center) {
                    if (index == 0) IconImage(R.drawable.design_neck_forward, Modifier.size(26.dp, 22.dp))
                    else if (index == 2) IconImage(R.drawable.design_level, Modifier.size(18.dp))
                    else Icon(listOf(R.drawable.design_neck_forward, R.drawable.legs, R.drawable.design_level, R.drawable.clock, R.drawable.monitor)[index], Modifier.size(18.dp))
                }
                Spacer(Modifier.width(10.dp))
                Column(Modifier.weight(1f)) { Label(row.first, 14, Ink, FontWeight.Bold); Label(row.second, 12, Muted) }
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
            DesignCard(Modifier.fillMaxWidth(), padding = 14) {
                Row { Label("기기 상태", 13, Ink, FontWeight.Bold, Modifier.weight(1f)); Pill(if (connected) "● 시연 연결" else "● 연결 대기") }
                SettingRow("카메라", if (connected) "정면 설치 · 시연 상태" else "연결 대기", R.drawable.camera)
                SettingRow("압력센서 구성", "FSR 6개 · 시연", R.drawable.sliders)
                SettingRow("방석 압력 센서", if (connected) "시연 데이터" else "연결 대기", R.drawable.seat)
                SettingRow("적외선 거리 센서", if (connected) "시연 데이터" else "연결 대기", R.drawable.distence)
                SettingRow("웹 연결", "실제 통신 미연동", R.drawable.wifi)
                SettingRow("기기 재연결", "›", R.drawable.refresh, onClick = { onAction("기기 재연결") })
            }
        }
        item { Label("측정", 11, Muted, FontWeight.Bold) }
        item {
            DesignCard(Modifier.fillMaxWidth(), padding = 13) {
                SettingRow("개선 목표", if (goals.isEmpty()) "선택 안 함" else "${goalNames[goals.first()]} 외 ${goals.size - 1}개", R.drawable.target, onClick = { onAction("개선 목표") })
                SettingRow("기준 자세 다시 측정", "›", R.drawable.refresh, onClick = { onAction("기준 자세 다시 측정") })
                SettingRow("자세 인정 기준", "3초 이상 유지 ›", R.drawable.clock, onClick = { onAction("자세 인정 기준") })
            }
        }
        item { Label("알림", 11, Muted, FontWeight.Bold) }
        item {
            DesignCard(Modifier.fillMaxWidth(), padding = 13) {
                SettingRow("실시간 자세 알림", "", R.drawable.bell, toggle = alerts, onToggle = setAlerts)
                SettingRow("알림 기준", "${threshold}분 이상 지속 시 ›", R.drawable.clock, onClick = { onAction("알림 기준") })
                SettingRow("일어나기 알림", "${standMinutes}분 연속 착석 시", R.drawable.walk, toggle = standAlert, onToggle = setStandAlert, onClick = { onAction("일어나기 알림") })
            }
        }
        item { Label("데이터 · 프라이버시", 11, Muted, FontWeight.Bold) }
        item {
            DesignCard(Modifier.fillMaxWidth(), padding = 13) {
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
                Column(Modifier.weight(1f)) { Label("기기 연결 상태", 12, if (connected) Ink else Soft); Label(if (connected) "시연 연결이 완료됐어요." else "연결되어 있지 않아요.", 20, if (connected) Ink else Color.White, FontWeight.Bold) }
                Box(Modifier.size(56.dp).clip(RoundedCornerShape(14.dp)).background(if (connected) Mint else Color(0xFF777B80)), contentAlignment = Alignment.Center) { Icon(R.drawable.user, Modifier.size(25.dp), if (connected) Green else Slate) }
            }
        }
        item { Label("기기 연결 상태", 15, Ink, FontWeight.Bold) }
        item {
            DesignCard(Modifier.fillMaxWidth(), padding = 15) {
                listOf(Triple("카메라", "시연 상태", R.drawable.camera), Triple("방석 압력 센서", "시연 데이터", R.drawable.seat), Triple("적외선 거리 센서", "시연 데이터", R.drawable.distence)).forEachIndexed { index, row ->
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
            DesignCard(Modifier.fillMaxWidth(), padding = 18) {
                Label("바른자리", 15, Ink, FontWeight.Bold)
                Label("시연용 기기", 12, Muted)
                Spacer(Modifier.height(11.dp))
                PrimaryButton(if (connected) "기기 연결 끊기" else "이 기기 연결하기", toggleConnection, modifier = Modifier.height(42.dp), color = if (connected) Danger else Green)
            }
        }
        item { Label("ⓘ 현재 버튼은 연결 상태를 시연해요. 실제 기기 통신은 아직 연결되지 않았어요.", 11, Soft) }
    }
}


@Composable
private fun ConnectionOnboardingScreen(includeConnection: Boolean, onBack: () -> Unit, onDone: () -> Unit) {
    val duration = 7_000L
    val startedAt = rememberSaveable { SystemClock.elapsedRealtime() }
    var elapsed by remember { mutableLongStateOf((SystemClock.elapsedRealtime() - startedAt).coerceAtLeast(0L)) }
    val latestDone by rememberUpdatedState(onDone)
    val connecting = includeConnection
    val stageElapsed = elapsed
    val progress = (stageElapsed / 7_000f).coerceIn(0f, 1f)
    BackHandler(onBack = onBack)
    LaunchedEffect(startedAt) {
        while (elapsed < duration) {
            withFrameNanos { elapsed = (SystemClock.elapsedRealtime() - startedAt).coerceAtLeast(0L) }
        }
        latestDone()
    }
    Column(Modifier.fillMaxSize().background(Color.White).verticalScroll(rememberScrollState())
        .padding(horizontal = 20.dp, vertical = 16.dp), verticalArrangement = Arrangement.spacedBy(22.dp)) {
        StepHeader(if (connecting) 2 else 3, onBack, total = 3)
        Label(if (connecting) "기기와 센서를\n연결하고 있어요" else "바른 자세로\n편하게 앉아주세요", 25, Ink, FontWeight.Bold)
        Label(if (connecting) "카메라, 방석 압력 센서, 적외선 거리 센서의 연결을 확인하는 화면이에요. 잠시만 기다려 주세요." else "아래 가이드에 맞춘 뒤 약 7초 동안 편하게 앉아주세요. 완료 후 홈으로 이동해요.", 14, Muted)
        if (connecting) {
            IconImage(R.drawable.design_pairing, Modifier.fillMaxWidth().height(184.dp))
        } else Box(Modifier.fillMaxWidth().height(190.dp), contentAlignment = Alignment.Center) {
            Box(Modifier.size(168.dp), contentAlignment = Alignment.Center) {
                ProgressRing(progress * 100f, Modifier.fillMaxSize(), stroke = 12f)
                Column(horizontalAlignment = Alignment.CenterHorizontally) {
                    Label("${kotlin.math.ceil((7_000L - stageElapsed).coerceAtLeast(0L) / 1000.0).toInt()}", 54, Ink, FontWeight.Bold)
                    Label("초 남음", 13, Muted)
                }
            }
        }
        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.Center) {
            Pill(if (connecting) "● 기기 연결 중" else "● 기준 자세 설정 중")
        }
        if (connecting) Column(Modifier.fillMaxWidth().clip(RoundedCornerShape(18.dp)).background(Background).padding(16.dp)) {
            listOf(
                Triple("방석 압력 센서", "6개 · 시연", R.drawable.seat),
                Triple("적외선 거리 센서", "시연 데이터", R.drawable.distence),
                Triple("카메라", "센서 입력 미연동", R.drawable.camera),
                Triple("착석 감지", "시연 상태", R.drawable.user)
            ).forEach { (name, status, icon) ->
                Row(Modifier.fillMaxWidth().padding(vertical = 10.dp), verticalAlignment = Alignment.CenterVertically) {
                    Icon(icon, Modifier.size(20.dp))
                    Spacer(Modifier.width(12.dp))
                    Label(name, 13, Ink, FontWeight.SemiBold, Modifier.weight(1f))
                    Label(status, 11, Muted)
                }
            }
        } else Column(Modifier.fillMaxWidth().clip(RoundedCornerShape(18.dp)).background(Background).padding(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
            Label("이렇게 앉아 주세요", 16, Ink, FontWeight.Bold)
            listOf(
                "의자 깊숙이 앉아 등을 기대요" to "허리부터 어깨까지 등받이에 편하게 닿도록 앉아요",
                "두 발을 바닥에 붙이고 다리는 풀어요" to "발바닥을 바닥에 대고 다리를 꼬지 않아요",
                "어깨 힘을 빼고 수평을 맞춰요" to "팔은 자연스럽게 내리고 어깨를 편하게 둬요",
                "턱을 살짝 당기고 화면을 정면으로 봐요" to "목을 앞으로 내밀지 않고 편하게 바라봐요",
                "화면과 눈의 거리를 확인해요" to "화면에서 50cm 이상 떨어져 앉아주세요"
            ).forEachIndexed { index, (title, detail) ->
                Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                    Box(Modifier.size(22.dp).clip(CircleShape).background(PaleGreen), contentAlignment = Alignment.Center) { Label("${index + 1}", 12, Green, FontWeight.Bold) }
                    Column { Label(title, 13, Ink, FontWeight.SemiBold); Label(detail, 11, Muted) }
                }
                if (index < 4) HorizontalDivider(color = Line)
            }
        }
        LinearProgressIndicator(progress = { progress }, modifier = Modifier.fillMaxWidth(), color = Green, trackColor = PaleGreen)
        PrimaryButton(if (connecting) "연결 중…" else "측정 중…", {}, enabled = false)
        Label("현재는 실제 센서 연결 없이 진행하는 화면 체험이에요", 11, Soft,
            modifier = Modifier.fillMaxWidth(), align = TextAlign.Center)
    }
}

@Composable
private fun MeasurementPlayer(status: MeasurementStatus, millis: Long, onToggle: () -> Unit, onStop: () -> Unit, onOpen: () -> Unit) {
    Row(Modifier.fillMaxWidth().padding(horizontal = 12.dp).padding(top = 8.dp)
        .shadow(8.dp, RoundedCornerShape(20.dp), ambientColor = Color.Black.copy(alpha = .1f), spotColor = Color.Black.copy(alpha = .1f))
        .height(64.dp).clip(RoundedCornerShape(20.dp)).background(PaleGreen)
        .padding(start = 10.dp, end = 12.dp, top = 10.dp, bottom = 10.dp), verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(12.dp)) {
        Box(Modifier.size(44.dp).clip(RoundedCornerShape(14.dp)).background(Color.White).clickable(onClick = onOpen), contentAlignment = Alignment.Center) {
            IconImage(if (status == MeasurementStatus.PAUSED) R.drawable.player_leaf_paused else R.drawable.player_leaf, Modifier.size(26.dp, 25.dp))
        }
        Column(Modifier.weight(1f).clickable(onClick = onOpen), verticalArrangement = Arrangement.spacedBy(1.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(5.dp)) {
                if (status != MeasurementStatus.IDLE) Box(Modifier.size(6.dp).clip(CircleShape).background(if (status == MeasurementStatus.PAUSED) Soft else Green))
                Text(when (status) { MeasurementStatus.IDLE -> "오늘 측정 전"; MeasurementStatus.RUNNING -> "측정 중"; MeasurementStatus.PAUSED -> "일시정지" },
                    color = when (status) { MeasurementStatus.RUNNING -> Green; MeasurementStatus.PAUSED -> Soft; else -> Muted }, fontSize = 12.sp, lineHeight = 14.sp, fontWeight = FontWeight.Medium)
            }
            Text(if (status == MeasurementStatus.IDLE) "바르게 앉고 시작해요" else measurementTime(millis),
                fontSize = if (status == MeasurementStatus.IDLE) 15.sp else 18.sp,
                lineHeight = if (status == MeasurementStatus.IDLE) 18.sp else 22.sp,
                fontWeight = FontWeight.Bold, color = if (status == MeasurementStatus.PAUSED) Soft else PlayerInk)
        }
        Row(horizontalArrangement = Arrangement.spacedBy(6.dp), verticalAlignment = Alignment.CenterVertically) {
            if (status != MeasurementStatus.IDLE) {
                Box(Modifier.size(38.dp).clip(CircleShape).background(Color.White)
                    .semantics { contentDescription = "측정 종료" }.clickable(onClick = onStop), contentAlignment = Alignment.Center) {
                    Box(Modifier.size(11.dp).clip(RoundedCornerShape(2.5.dp)).background(PlayerInk))
                }
            }
            if (status == MeasurementStatus.IDLE) {
                Row(Modifier.size(71.35.dp, 37.dp).clip(CircleShape).background(Green)
                    .semantics { contentDescription = "자세 측정 시작" }.clickable(onClick = onToggle),
                    horizontalArrangement = Arrangement.Center, verticalAlignment = Alignment.CenterVertically) {
                    IconImage(R.drawable.player_play, Modifier.size(9.35.dp, 11.dp))
                    Spacer(Modifier.width(6.dp))
                    Text("시작", fontSize = 14.sp, color = Color.White, fontWeight = FontWeight.Bold)
                }
            } else {
                Box(Modifier.size(44.dp).clip(CircleShape).background(Green)
                    .semantics { contentDescription = if (status == MeasurementStatus.RUNNING) "측정 일시정지" else "측정 다시 시작" }
                    .clickable(onClick = onToggle), contentAlignment = Alignment.Center) {
                    if (status == MeasurementStatus.RUNNING) {
                        Row(Modifier.size(12.dp, 14.dp), horizontalArrangement = Arrangement.spacedBy(4.dp)) {
                            repeat(2) { Box(Modifier.size(4.dp, 14.dp).clip(RoundedCornerShape(1.5.dp)).background(Color.White)) }
                        }
                    } else IconImage(R.drawable.player_play, Modifier.size(12.dp, 14.dp))
                }
            }
        }
    }
}

@Composable
private fun MeasurementScreen(status: MeasurementStatus, millis: Long, onBack: () -> Unit) {
    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(20.dp), verticalArrangement = Arrangement.spacedBy(20.dp)) {
        TextButton(onClick = onBack) { Text("‹ 홈으로", color = Green) }
        Label("실시간 자세 측정", 24, Ink, FontWeight.Bold)
        RoundedCard(Modifier.fillMaxWidth()) {
            Label(if (status == MeasurementStatus.RUNNING) "측정 중" else if (status == MeasurementStatus.PAUSED) "일시정지" else "측정 종료", 14, Green)
            Label(measurementTime(millis), 36, Ink, FontWeight.Bold)
            Spacer(Modifier.height(20.dp))
            IconImage(R.drawable.home_seated_pose, Modifier.size(126.dp, 94.dp))
            Spacer(Modifier.height(16.dp))
            Label("자세 데이터 연결 대기", 17, Ink, FontWeight.Bold)
            Label("측정 시간과 시작·일시정지·종료 기능을 체험할 수 있어요. 실제 자세 판정과 센서 데이터는 아직 연결하지 않았어요.", 13, Muted)
        }
        Label("아래 플레이어로 측정을 제어할 수 있어요. 다른 메뉴로 이동해도 측정 시간은 유지돼요.", 13, Muted)
    }
}

@Composable
private fun DesignCard(modifier: Modifier = Modifier, padding: Int = 18, content: @Composable ColumnScope.() -> Unit) {
    val shape = RoundedCornerShape(22.dp)
    Column(modifier.shadow(4.dp, shape, ambientColor = Color.Black.copy(alpha = .04f), spotColor = Color.Black.copy(alpha = .04f))
        .clip(shape).background(Color.White).padding(padding.dp), content = content)
}

@Composable
private fun PostureGauges() {
    Column(Modifier.fillMaxWidth().height(112.dp).clip(RoundedCornerShape(12.dp)).background(Background).padding(8.dp)) {
        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
            Column(Modifier.weight(1f), horizontalAlignment = Alignment.CenterHorizontally) {
                IconImage(R.drawable.design_neck_normal, Modifier.size(36.dp, 30.dp))
                Label("목 정상", 10, Muted)
            }
            Box(Modifier.width(1.dp).height(46.dp).background(Line))
            Column(Modifier.weight(1f), horizontalAlignment = Alignment.CenterHorizontally) {
                Box(Modifier.width(58.dp).height(18.dp).clip(CircleShape).background(Color.White).border(1.dp, Line, CircleShape), contentAlignment = Alignment.Center) {
                    Row(horizontalArrangement = Arrangement.spacedBy(15.dp)) {
                        repeat(2) { Box(Modifier.width(1.dp).height(12.dp).background(Line)) }
                    }
                    Box(Modifier.size(12.dp).clip(CircleShape).background(Green))
                }
                Spacer(Modifier.height(5.dp))
                Label("몸 바름", 10, Muted)
            }
        }
        Spacer(Modifier.height(3.dp))
        Label("화면 거리 52cm", 10, Muted)
        Box(Modifier.fillMaxWidth().height(13.dp), contentAlignment = Alignment.Center) {
            Row(Modifier.fillMaxWidth().height(6.dp).clip(CircleShape)) {
                Box(Modifier.weight(.2f).fillMaxHeight().background(Line))
                Box(Modifier.weight(.2f).fillMaxHeight().background(Color(0xFFBFE5CC)))
                Box(Modifier.weight(.6f).fillMaxHeight().background(Green))
            }
            Box(Modifier.size(12.dp).clip(CircleShape).background(Color.White).border(3.dp, Green, CircleShape))
        }
        Label("     40      50", 8, Soft)
    }
}

@Composable
@OptIn(ExperimentalMaterial3Api::class)
private fun ReportCalendar(selectedDate: Long, weekly: Boolean, onDismiss: () -> Unit, onSelect: (Long) -> Unit) {
    var pendingDate by rememberSaveable { mutableLongStateOf(selectedDate) }
    var month by rememberSaveable { mutableLongStateOf(ReportDates.monthStart(selectedDate)) }
    val today = ReportDates.today()
    ModalBottomSheet(onDismissRequest = onDismiss, sheetState = rememberModalBottomSheetState(skipPartiallyExpanded = true),
        containerColor = Color.White, shape = RoundedCornerShape(topStart = 24.dp, topEnd = 24.dp)) {
        Column(Modifier.fillMaxWidth().verticalScroll(rememberScrollState()).padding(horizontal = 20.dp).padding(bottom = 24.dp), verticalArrangement = Arrangement.spacedBy(16.dp)) {
            Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                IconButton(onClick = { month = ReportDates.shiftMonth(month, -1) }, modifier = Modifier.semantics { contentDescription = "이전 달" }) { Icon(R.drawable.chevl, Modifier.size(20.dp), Ink) }
                Label(ReportDates.format(month, "yyyy년 M월"), 20, Ink, FontWeight.Bold, Modifier.weight(1f), TextAlign.Center)
                IconButton(onClick = { month = ReportDates.shiftMonth(month, 1) }, enabled = month < ReportDates.monthStart(today), modifier = Modifier.semantics { contentDescription = "다음 달" }) { Icon(R.drawable.chevr, Modifier.size(20.dp), Ink) }
            }
            Row(Modifier.fillMaxWidth()) {
                listOf("일", "월", "화", "수", "목", "금", "토").forEach { Label(it, 12, Soft, modifier = Modifier.weight(1f), align = TextAlign.Center) }
            }
            ReportDates.monthCells(month).chunked(7).forEach { week ->
                Row(Modifier.fillMaxWidth()) {
                    week.forEach { date ->
                        Box(Modifier.weight(1f).height(42.dp), contentAlignment = Alignment.Center) {
                            if (date != null) {
                                val chosen = date == pendingDate
                                Column(Modifier.size(40.dp).clip(CircleShape).background(if (chosen) Green else Color.Transparent)
                                    .semantics { contentDescription = ReportDates.format(date, "yyyy년 M월 d일") }
                                    .clickable(enabled = date <= today) { pendingDate = date }, horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.Center) {
                                    Label(ReportDates.format(date, "d"), 15, if (chosen) Color.White else if (date > today) Line else if (date == today) Green else Ink, if (chosen || date == today) FontWeight.Bold else FontWeight.Normal)
                                    if (date == today) Box(Modifier.width(14.dp).height(2.dp).background(if (chosen) Color.White else Green))
                                }
                            }
                        }
                    }
                }
            }
            Label(if (weekly) "선택한 날짜가 속한 월요일~일요일 리포트를 표시해요" else "선택한 날짜의 일간 리포트를 표시해요", 12, Muted, modifier = Modifier.fillMaxWidth(), align = TextAlign.Center)
            PrimaryButton("${ReportDates.format(pendingDate, "M월 d일")} 리포트 보기", { onSelect(pendingDate); onDismiss() }, color = Green)
        }
    }
}
