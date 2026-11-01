package com.example.barunjari

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.Image
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.Alignment
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import org.json.JSONObject
import kotlin.math.ceil

private val ServerGreen = Color(0xFF16A34A)
private val ServerMuted = Color(0xFF6B7280)
private fun JSONObject.number(key: String): Double? = if (isNull(key)) null else optDouble(key).takeIf { it.isFinite() }
private fun seconds(value: Double?): String = value?.let { "${(it / 60).toInt()}분 ${it.toInt() % 60}초" } ?: "기록 없음"
private fun percent(value: Double?): String = value?.let { "${kotlin.math.round(it * 100).toInt()}%" } ?: "판정 기록 없음"

@Composable internal fun ServerDevice(status: JSONObject, connected: Boolean, busy: Boolean, toggle: () -> Unit) {
    ServerColumn {
        Label("기기 연결", 24, weight = FontWeight.Bold)
        Label(if (connected) "${status.optString("mode")} 서버 연결" else "기기를 연결해 주세요", 18)
        val sensors = status.optJSONObject("sensors")
        listOf("camera" to "카메라", "pressure" to "방석 압력 센서", "distance" to "적외선 거리 센서").forEach { (key, name) ->
            val sensor = sensors?.optJSONObject(key)
            DesignCard(Modifier.fillMaxWidth()) {
                Label(name, 17, weight = FontWeight.Bold)
                Label(if (!connected) "연결 대기" else if (sensor?.optBoolean("ok") == true) "정상 응답" else "연결 확인 필요", 14)
            }
        }
        PrimaryButton(if (connected) "연결 끊기" else "서버 주소로 연결", toggle, enabled = !busy, color = ServerGreen)
        Label("주변 기기 자동 검색은 서버 API가 아직 제공되지 않았어요", 12, ServerMuted)
    }
}

@Composable private fun ServerColumn(content: @Composable ColumnScope.() -> Unit) {
    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(20.dp),
        verticalArrangement = Arrangement.spacedBy(14.dp), content = content)
}

@Composable internal fun ServerConnect(address: String, change: (String) -> Unit, busy: Boolean, error: String, onBack: () -> Unit, onConnect: () -> Unit) {
    ServerColumn {
        TextButton(onClick = onBack) { Text("뒤로") }
        Label("기기 연결", 25, weight = FontWeight.Bold)
        Label("Pi와 같은 Wi-Fi에 연결해 주세요", 14, ServerMuted)
        OutlinedTextField(address, change, label = { Text("서버 주소") }, singleLine = true, enabled = !busy, modifier = Modifier.fillMaxWidth())
        if (error.isNotBlank()) Label(error, 13, Color(0xFFE85050))
        if (busy) LinearProgressIndicator(Modifier.fillMaxWidth())
        PrimaryButton(if (busy) "연결 확인 중…" else "기기 연결", onConnect, enabled = !busy, color = ServerGreen)
    }
}

@Composable internal fun ServerCalibration(session: JSONObject, busy: Boolean, error: String, onBack: () -> Unit, onStart: () -> Unit, onDone: () -> Unit) {
    val baseline = session.optJSONObject("baseline") ?: JSONObject()
    val state = baseline.optString("state", "none")
    val latestStart by rememberUpdatedState(onStart)
    val latestDone by rememberUpdatedState(onDone)
    var sawMeasurement by remember { mutableStateOf(false) }
    LaunchedEffect(Unit) { latestStart() }
    LaunchedEffect(state) {
        if (state in listOf("waiting_seat", "measuring")) sawMeasurement = true
        if (state == "ready" && sawMeasurement) latestDone()
    }
    ServerColumn {
        TextButton(onClick = onBack) { Text("뒤로") }
        Label("기준 자세 설정", 25, weight = FontWeight.Bold)
        Label(baselineMessage(state, baseline.optString("fail_reason")), 20, weight = FontWeight.Bold)
        baseline.number("remaining_sec")?.let { Label("${ceil(it).toInt()}초 남음", 30, ServerGreen) }
        Label("방석에 깊숙이 앉고 다리를 풀어 주세요. 화면에서 50cm 이상 떨어져 상체가 카메라에 보이게 해 주세요.", 14, ServerMuted)
        if (state in listOf("waiting_seat", "measuring") || busy) LinearProgressIndicator(Modifier.fillMaxWidth())
        if (baseline.optBoolean("using_previous")) Label("직전 성공 기준을 사용하고 있어요", 13)
        if (error.isNotBlank()) Label(error, 13, Color(0xFFE85050))
        PrimaryButton(if (state == "ready") "홈으로" else "다시 측정", if (state == "ready") onDone else onStart,
            enabled = !busy && state !in listOf("waiting_seat", "measuring"), color = ServerGreen)
    }
}

@Composable internal fun ServerHome(status: JSONObject, current: JSONObject, layout: JSONObject, report: JSONObject, error: String) {
    ServerColumn {
        Label("바른자리", 24, weight = FontWeight.Bold)
        Label(ReportDates.format(ReportDates.today(), "M월 d일 EEEE"), 13, ServerMuted)
        if (status.optString("mode") == "mock") Label("예시 데이터 · mock 서버", 13, ServerGreen)
        if (error.isNotBlank()) Label("연결 확인 필요 · $error", 13, Color(0xFFE85050))
        DesignCard(Modifier.fillMaxWidth()) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Column(Modifier.weight(1f)) {
                    Label("하루 평균 자세", 12, ServerMuted)
                    Label(postureName(report.optJSONObject("daily_posture")?.optString("posture") ?: "unknown"), 24, weight = FontWeight.Bold)
                }
                Image(painterResource(R.drawable.design_summary_plant), null, Modifier.size(86.dp))
            }
        }
        DesignCard(Modifier.fillMaxWidth()) {
            Label("오늘 정상 자세 비율", 14, ServerMuted)
            Label(percent(report.number("normal_ratio")), 36, ServerGreen, FontWeight.Bold)
            Label("총 착석 ${seconds(report.number("seated_sec"))} · 정상 ${seconds(report.number("normal_sec"))}", 12)
            Label("판정 불가 시간은 비율 계산에서 제외해요", 11, ServerMuted)
            val ratio = report.number("normal_ratio")?.toFloat() ?: 0f
            LinearProgressIndicator(progress = { ratio.coerceIn(0f, 1f) }, modifier = Modifier.fillMaxWidth(), color = ServerGreen)
        }
        val pending = current.optJSONArray("pending")
        if (pending != null && pending.length() > 0) DesignCard(Modifier.fillMaxWidth()) {
            Label("판정 대기 중", 17, ServerGreen, FontWeight.Bold)
            Label("자세가 유지되는지 확인하고 있어요. 반영까지 약 5초 걸릴 수 있어요.", 12, ServerMuted)
            repeat(pending.length()) { i ->
                val item = pending.optJSONObject(i) ?: return@repeat
                val kind = when(item.optString("kind")) { "seat" -> "좌면"; "head" -> "목"; else -> "몸 기울기" }
                Label("$kind · ${postureName(item.optString("state"))} · ${ceil(item.optDouble("remaining_sec", 0.0)).toInt()}초 남음", 13)
                val progress by androidx.compose.animation.core.animateFloatAsState(
                    targetValue = (1f - item.optDouble("remaining_sec", 4.0).toFloat() / 4f).coerceIn(0f, 1f),
                    animationSpec = androidx.compose.animation.core.tween(1000), label = "pending")
                LinearProgressIndicator(progress = { progress }, modifier = Modifier.fillMaxWidth(), color = ServerGreen)
            }
        }
        DesignCard(Modifier.fillMaxWidth()) {
            Label("지금 자세 · 확정된 판정", 17, weight = FontWeight.Bold)
            if (current.length() == 0) Label("측정 데이터가 아직 없어요", 14, ServerMuted)
            else {
                Label("목 · ${postureName(current.optString("head_state"))}", 15)
                Label("몸 · ${postureName(current.optString("tilt_state"))}", 15)
                Label("좌면 · ${postureName(current.optString("seat_state"))}", 15)
                Label("화면 거리 · ${current.number("distance_mm")?.let { "${it / 10}cm" } ?: "측정 불가"}", 15)
                Label(postureName(current.optString("distance_level")), 13, ServerMuted)
            }
        }
        DesignCard(Modifier.fillMaxWidth()) {
            Label("방석 압력 · 센서 위치 기준", 17, weight = FontWeight.Bold)
            val channels = layout.optJSONArray("channel_map")
            val positions = layout.optJSONArray("positions")
            val ratios = current.optJSONArray("pressure_ratio")
            val pressure = current.optJSONArray("pressure")
            if (channels == null) Label("센서 배치를 불러오지 못했어요", 13)
            else repeat(channels.length()) { i ->
                val position = positions?.optJSONArray(i)
                val side = position?.optDouble(0)?.let { if (it < 0) "왼쪽" else if (it > 0) "오른쪽" else "가운데" }.orEmpty()
                val depth = position?.optDouble(1)?.let { if (it < 0) "뒤" else if (it > 0) "앞" else "중간" }.orEmpty()
                Label("CH${channels.optInt(i)} · $depth $side · ${ratios?.let { if (!it.isNull(i)) "${kotlin.math.round(it.optDouble(i) * 100).toInt()}%" else "—" } ?: "—"} · ${pressure?.optInt(i) ?: "—"}", 13)
            }
        }
        val goals = report.optJSONArray("goals")
        if (goals != null) DesignCard(Modifier.fillMaxWidth()) {
            Label("내 목표", 17, weight = FontWeight.Bold)
            repeat(goals.length()) { i -> val goal = goals.getJSONObject(i)
                Label("${goal.optString("label")} · 오늘 ${seconds(goal.number("today"))}", 13)
            }
        }
    }
}

@Composable internal fun ServerReport(report: JSONObject, error: String, weekly: Boolean, setWeekly: (Boolean) -> Unit, date: Long, selectDate: (Long) -> Unit) {
    var calendar by remember { mutableStateOf(false) }
    if (calendar) ReportCalendar(date, weekly, { calendar = false }, selectDate)
    ServerColumn {
        Label("리포트", 24, weight = FontWeight.Bold)
        Row { TextButton(onClick = { setWeekly(false) }) { Text("일간") }; TextButton(onClick = { setWeekly(true) }) { Text("주간") }; TextButton(onClick = { calendar = true }) { Text("달력") } }
        Row {
            TextButton(onClick = { selectDate(ReportDates.shift(date, if (weekly) -7 else -1)) }) { Text("‹") }
            TextButton(onClick = { calendar = true }) { Text(ReportDates.label(date, weekly)) }
            TextButton(enabled = ReportDates.shift(date, if (weekly) 7 else 1) <= ReportDates.today(), onClick = { selectDate(ReportDates.shift(date, if (weekly) 7 else 1)) }) { Text("›") }
        }
        if (error.isNotBlank()) Label(error, 13, Color(0xFFE85050))
        if (report.length() == 0) Label("기록을 기다리고 있어요", 14, ServerMuted)
        else {
            DesignCard(Modifier.fillMaxWidth()) {
                Label(if (weekly) "주간 정상 자세 비율" else "정상 자세 비율", 14)
                Label(percent(report.number("normal_ratio")), 36, ServerGreen, FontWeight.Bold)
                if (weekly) Label("지난주 대비 ${report.number("delta_pp")?.let { "%.1f%%p".format(it) } ?: "비교 기록 없음"}", 13)
                else {
                    Label("총 착석 ${seconds(report.number("seated_sec"))}", 15)
                    Label("최대 연속 착석 ${seconds(report.number("max_continuous_sec"))}", 15)
                    Label("자세 무너짐 시작 ${seconds(report.optJSONObject("collapse")?.number("avg_sec"))}", 15)
                    val distance = report.optJSONObject("distance")
                    Label("평균 거리 ${distance?.number("avg_mm")?.let { "${it / 10}cm" } ?: "기록 없음"}", 15)
                }
            }
            if (weekly) {
                val days = report.optJSONArray("days")
                DesignCard(Modifier.fillMaxWidth()) {
                    Label("요일별 착석시간", 17, weight = FontWeight.Bold)
                    if (days != null) repeat(days.length()) { i -> val day = days.getJSONObject(i)
                        Label("${day.optString("date")} · ${seconds(day.number("seated_sec"))} · 정상 ${seconds(day.number("normal_sec"))}", 13)
                    }
                }
                val compare = report.optJSONObject("compare")
                DesignCard(Modifier.fillMaxWidth()) {
                    Label("지난주와 비교 · 기록이 있는 날 평균", 16, weight = FontWeight.Bold)
                    listOf("forward_head" to "거북목", "cross" to "다리 꼬기", "tilt_direction" to "카메라 기울기", "max_continuous" to "최대 연속 착석", "distance_mm" to "화면 거리").forEach { (key, title) ->
                        val value = compare?.optJSONObject(key)
                        val previous = value?.number("last_week")
                        val now = value?.number("this_week")
                        Label("$title · ${if (key == "distance_mm") "${previous?.div(10) ?: "—"}cm → ${now?.div(10) ?: "—"}cm" else "${seconds(previous)} → ${seconds(now)}"}", 13)
                    }
                }
                report.number("worst_hour")?.let { hour -> DesignCard(Modifier.fillMaxWidth()) {
                    Label("이번 주 패턴", 16, weight = FontWeight.Bold)
                    Label("${hour.toInt()}시대의 비정상 자세 비율이 가장 높았어요", 14)
                } }
            } else {
                val postures = report.optJSONObject("postures")
                DesignCard(Modifier.fillMaxWidth()) {
                    Label("자세별 누적시간", 17, weight = FontWeight.Bold)
                    postures?.keys()?.forEach { key -> Label("${postureName(key)} · ${seconds(postures.number(key))}", 14) }
                }
                val balance = report.optJSONObject("lean_balance")
                DesignCard(Modifier.fillMaxWidth()) {
                    Label("좌우 체중 편향 비교", 17, weight = FontWeight.Bold)
                    Label("왼쪽 ${seconds(balance?.number("left_sec"))} · 오른쪽 ${seconds(balance?.number("right_sec"))}", 14)
                    Label("기준보다 10cm 이상 가까웠던 시간 ${seconds(report.optJSONObject("distance")?.number("closer_sec"))}", 13)
                }
            }
        }
    }
}

@Composable internal fun ServerGoals(report: JSONObject, error: String, selected: Set<Int>, edit: () -> Unit) {
    ServerColumn {
        Label("피드백", 24, weight = FontWeight.Bold)
        TextButton(onClick = edit) { Text("목표 수정") }
        if (error.isNotBlank()) Label(error, 13, Color(0xFFE85050))
        val keys = listOf("cross", "forward_head", "lean", "long_sitting")
        val goals = report.optJSONArray("goals")
        if (goals == null) Label("목표별 기록을 기다리고 있어요", 14)
        else repeat(goals.length()) { i -> val goal = goals.getJSONObject(i)
            if (keys.indexOf(goal.optString("goal")) in selected) DesignCard(Modifier.fillMaxWidth()) {
                Label(goal.optString("label"), 18, weight = FontWeight.Bold)
                Label("오늘 ${seconds(goal.number("today"))} · 어제 ${seconds(goal.number("yesterday"))}", 14)
                Label("지난주 평균 ${seconds(goal.number("last_week_avg"))}", 13, ServerMuted)
                val days = goal.optJSONArray("last7")
                if (days != null) Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceEvenly, verticalAlignment = Alignment.Bottom) {
                    repeat(days.length()) { index -> val day = days.getJSONObject(index)
                        val value = day.number("value")
                        Column(horizontalAlignment = Alignment.CenterHorizontally) {
                            Label(value?.let { "${(it / 60).toInt()}분" } ?: "—", 9)
                            Box(Modifier.width(16.dp).height(((value ?: 0.0) / 60).coerceIn(2.0, 70.0).dp).clip(RoundedCornerShape(4.dp)).background(ServerGreen))
                            Label(day.optString("date").takeLast(2), 9, ServerMuted)
                        }
                    }
                }
                Label(when(goal.optString("goal")) { "cross" -> "두 발을 바닥에 붙이고 다리를 풀어 주세요"; "forward_head" -> "턱을 살짝 당기고 화면을 정면으로 봐 주세요"; "lean" -> "양쪽 좌면에 체중을 고르게 실어 주세요"; else -> "잠시 일어나 가볍게 걸어 주세요" }, 14)
            }
        }
    }
}
