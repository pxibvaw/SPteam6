package com.example.barunjari

import org.junit.Assert.*
import org.junit.Test

class PostureRulesTest {
    @Test fun weightBiasAndCameraTiltAreNotNormalAndPriorityIsExclusive() {
        assertEquals(Posture.WEIGHT_BIAS, PostureRules.dominant(false, false, true, false))
        assertEquals(Posture.BODY_TILT, PostureRules.dominant(false, false, false, true))
        assertEquals(Posture.FORWARD_NECK, PostureRules.dominant(true, true, true, true))
        assertEquals(Posture.CROSSED_LEGS, PostureRules.dominant(false, true, true, true))
        assertEquals(Posture.NORMAL, PostureRules.dominant(false, false, false, false))
    }
    @Test fun weeklyRatioWeightsSeatedTimeAndIgnoresEmptyDaysInAverage() {
        assertEquals(10.0, PostureRules.normalPercent(listOf(10, 0), listOf(10, 90)), .001)
        assertEquals(0.0, PostureRules.normalPercent(listOf(0), listOf(0)), .001)
        assertEquals(15.0, PostureRules.recordedDayAverage(listOf(10, null, 20))!!, .001)
        assertNull(PostureRules.recordedDayAverage(listOf(null)))
    }
    @Test fun demoExclusiveTimesEqualTotalAndHabitExcludesNormal() {
        assertEquals(252, DemoPostureReport.totalMinutes)
        assertEquals(Posture.FORWARD_NECK, DemoPostureReport.habit)
        assertNull(PostureRules.mostCommonHabit(mapOf(Posture.NORMAL to 100)))
    }
    @Test(expected = IllegalArgumentException::class)
    fun invalidNormalTimeCannotExceedSeatedTime() {
        PostureRules.normalPercent(listOf(11), listOf(10))
    }
}
