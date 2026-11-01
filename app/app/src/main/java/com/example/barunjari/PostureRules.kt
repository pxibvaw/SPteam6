package com.example.barunjari

/** Local calculations only. These types do not imply a connected sensor or an API response. */
internal enum class Posture(val label: String) {
    NORMAL("정상 자세"), FORWARD_NECK("거북목 자세"), CROSSED_LEGS("다리 꼬기"),
    WEIGHT_BIAS("체중 편향"), BODY_TILT("몸 기울기")
}

internal object PostureRules {
    const val TIME_ZONE = "Asia/Seoul"
    const val PRESSURE_SENSOR_COUNT = 6
    const val BASELINE_DISTANCE_CM = 50
    const val CONFIRMATION_MILLIS = 3_000L
    const val ABSENCE_STOP_MILLIS = 5_000L

    // Camera tilt and cushion bias remain separate; only one category receives exclusive time.
    fun dominant(neckForward: Boolean, legsCrossed: Boolean, weightBiased: Boolean, bodyTilted: Boolean): Posture =
        when {
            neckForward -> Posture.FORWARD_NECK
            legsCrossed -> Posture.CROSSED_LEGS
            weightBiased -> Posture.WEIGHT_BIAS
            bodyTilted -> Posture.BODY_TILT
            else -> Posture.NORMAL
        }

    fun normalPercent(normalMinutes: List<Int>, seatedMinutes: List<Int>): Double {
        require(normalMinutes.size == seatedMinutes.size)
        require(normalMinutes.zip(seatedMinutes).all { (normal, total) -> normal >= 0 && normal <= total })
        val total = seatedMinutes.sumOf { it.toLong() }
        return if (total == 0L) 0.0 else normalMinutes.sumOf { it.toLong() } * 100.0 / total
    }

    fun mostCommonHabit(exclusiveMinutes: Map<Posture, Int>): Posture? {
        require(exclusiveMinutes.values.all { it >= 0 })
        return exclusiveMinutes.filter { it.key != Posture.NORMAL && it.value > 0 }
            .maxByOrNull { it.value }?.key
    }

    fun recordedDayAverage(values: List<Int?>): Double? {
        require(values.filterNotNull().all { it >= 0 })
        return values.filterNotNull().takeIf { it.isNotEmpty() }?.average()
    }
}

/** Explicit demo fixture. Goal times can overlap and are kept separate from exclusive totals. */
internal object DemoPostureReport {
    val exclusiveMinutes = linkedMapOf(Posture.NORMAL to 181, Posture.FORWARD_NECK to 42,
        Posture.CROSSED_LEGS to 17, Posture.WEIGHT_BIAS to 12)
    val totalMinutes = exclusiveMinutes.values.sum()
    val normalPercent = kotlin.math.round(PostureRules.normalPercent(listOf(181), listOf(totalMinutes))).toInt()
    val habit = PostureRules.mostCommonHabit(exclusiveMinutes)!!
    val weeklySeatedMinutes = listOf(312, 366, 408, 294, 330, 180, 252)
    val weeklyNormalMinutes = listOf(203, 238, 265, 191, 215, 117, 181)
    val weeklyPercent = kotlin.math.round(PostureRules.normalPercent(weeklyNormalMinutes, weeklySeatedMinutes)).toInt()
}
